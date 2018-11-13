import asyncio
import datetime
import ipaddress
import logging
import os
import socket
import struct
import time

import aiocoap
import aiocoap.resource as resource
import kibra.database as db
import kibra.ksh as KSH
from aiocoap.numbers.codes import Code
from aiocoap.numbers.types import Type
from kibra.coapclient import CoapClient
from kibra.ktask import Ktask
from kibra.shell import bash
from kibra.thread import DEFS, TLV, URI
from kibra.tlv import ThreadTLV
from pyroute2 import IPRoute

# Global variables
IP = IPRoute()
MCAST_HNDLR = None

class MulticastHandler():
    def __init__(self):
        # Volatile multicast addresses list
        self.maddrs = {}

        # Load permanent addresses
        maddrs_perm = db.get('maddrs_perm') or []
        for addr in maddrs_perm:
            self.addr_add(addr, datetime.datetime.max)

        # Initialize smcroute
        bash('smcroute -d')

    def reg_update(self, addrs, addr_tout):
        for addr in addrs:
            if addr_tout > 0:
                self.addr_add(str(addr), addr_tout)
            elif str(addr) in self.maddrs.keys():
                self.addr_remove(str(addr))
        db.set('mlr_cache', str(self.maddrs))

    def addr_add(self, addr, addr_tout):
        if addr_tout == 0xffffffff:
            tout = datetime.datetime.max
            # TODO: persistent storage
        else:
            if addr_tout < DEFS.MIN_MLR_TIMEOUT:
                addr_tout = DEFS.MIN_MLR_TIMEOUT
            tout = datetime.datetime.now().timestamp() + addr_tout

        # Update smcroute configuration
        if str(addr) not in self.maddrs.keys():
            bash('smcroutectl join %s %s' % (db.get('exterior_ifname'), addr))
            bash('smcroutectl add %s :: %s %s' %
                 (db.get('exterior_ifname'), addr, db.get('interior_ifname')))

        # Save the new address in the volatile list
        self.maddrs[str(addr)] = tout

        logging.info('Multicast address %s registration updated (+%d s)' %
                     (addr, addr_tout))

    def addr_remove(self, addr):
        # Update smcroute configuration
        bash('smcroutectl remove %s :: %s' % (db.get('exterior_ifname'), addr))
        bash('smcroutectl leave %s %s' % (db.get('exterior_ifname'), addr))

        self.maddrs.pop(addr)
        # TODO: remove persistent

        logging.info('Multicast address %s registration removed.' % addr)

    def reg_periodic(self):
        now = datetime.datetime.now().timestamp()
        rem_list = [addr for addr, tout in self.maddrs.items() if tout < now]
        for addr in rem_list:
            self.addr_remove(addr)


class Res_N_MR(resource.Resource):
    '''Multicast registration, Thread 1.2 5.24'''

    # Defined statuses
    ST_SUCESS = 0
    ST_INV_ADDR = 2
    ST_RES_SHRT = 4
    ST_NOT_PRI = 5
    ST_UNSPEC = 6

    @staticmethod
    def _parse_addrs(tlv):
        addrs = []
        i = 0
        while i < tlv.length:
            try:
                addr = ipaddress.IPv6Address(bytes(tlv.value[i:i + 16]))
                if addr.is_multicast and tlv.value[i + 1] & 0x0F > 3:
                    addrs.append(addr)
            except:
                return Res_N_MR.ST_INV_ADDR, []
            i += 16
        return Res_N_MR.ST_SUCESS, addrs

    async def render_post(self, request):
        status = Res_N_MR.ST_UNSPEC

        # Incoming TLVs parsing
        in_pload = ThreadTLV(data=request.payload)
        logging.info('%s req: %s' % (URI.N_MR, in_pload))

        # BBR Primary/Secondary status
        if 'primary' not in db.get('bbr_status'):
            status = Res_N_MR.ST_NOT_PRI
        else:
            addrs = []
            timeout = None
            comm_sid = None

            for tlv in ThreadTLV.sub_tlvs(request.payload):
                if tlv.type is TLV.A_IPV6_ADDRESSES and tlv.length % 16 is 0:
                    ipv6_addressses_tlv = tlv
                    status, addrs = Res_N_MR._parse_addrs(tlv)
                elif tlv.type is TLV.A_TIMEOUT and tlv.length == 4:
                    timeout = tlv.value
                elif tlv.type is TLV.A_COMMISSIONER_SESSION_ID and tlv.length == 2:
                    comm_sid = tlv.value

            # Register valid addresses
            if addrs:
                if timeout and comm_sid:
                    addr_tout = timeout
                else:
                    addr_tout = db.get('mlr_timeout') or DEFS.MIN_MLR_TIMEOUT
                MCAST_HNDLR.reg_update(addrs, addr_tout)
                # Send BMLR.ntf
                timeout_tlv = ThreadTLV(
                    t=TLV.A_TIMEOUT, l=4, v=struct.pack('!I', addr_tout))
                payload = ipv6_addressses_tlv.array() + timeout_tlv.array()
                dst = '%s%%%s' % (db.get('all_network_bbrs'),
                                  db.get('exterior_ifname'))
                client = CoapClient()
                await client.non_request(dst, DEFS.PORT_BB, URI.B_BMR, payload)
                client.stop()

        # Fill and return the response
        out_pload = ThreadTLV(t=TLV.A_STATUS, l=1, v=[status])
        code = Code.CHANGED
        payload = out_pload.array()
        logging.info('%s rsp: %s' % (URI.N_MR, out_pload))
        return aiocoap.Message(code=code, payload=payload)


class Res_B_BMR(resource.Resource):
    '''Backbone Multicast Listener Report Notification, Thread 1.2 9.4.8.4.5'''

    async def render_post(self, request):
        # Incoming TLVs parsing
        in_pload = ThreadTLV(data=request.payload)
        logging.info('%s req: %s' % (URI.B_BMR, in_pload))

        # Primary BBR shouldn't receive this message
        if 'primary' in db.get('bbr_status'):
            return

        addrs = []
        timeout = None
        for tlv in ThreadTLV.sub_tlvs(request.payload):
            if tlv.type is TLV.A_IPV6_ADDRESSES and tlv.length % 16 is 0:
                _, addrs = Res_N_MR._parse_addrs(tlv)
            elif tlv.type is TLV.A_TIMEOUT and tlv.length == 4:
                timeout = tlv.value

        # Register valid addresses
        if addrs and timeout:
            MCAST_HNDLR.reg_update(addrs, timeout)


class CoapServer():
    '''CoAP Server'''

    def __init__(self, addr, port, resources):
        root = resource.Site()
        for res in resources:
            root.add_resource(res[0], res[1])
        self.task = asyncio.Task(
            aiocoap.Context.create_server_context(root, bind=(addr, port)))

    def stop(self):
        self.task.cancel()


class COAPSERVER(Ktask):
    def __init__(self):
        Ktask.__init__(
            self,
            name='coapserver',
            start_keys=['dongle_rloc', 'dongle_prefix'],
            start_tasks=['serial', 'network', 'diags'],
            stop_tasks=['network'],
            period=5)

    def kstart(self):
        global MCAST_HNDLR
        MCAST_HNDLR = MulticastHandler()

        # Set All Network BBRs multicast address as per 9.4.8.1
        int_addr = int('ff320040' + db.get('dongle_prefix') + '00000003', 16)
        all_network_bbrs = ipaddress.IPv6Address(int_addr).compressed
        db.set('all_network_bbrs', all_network_bbrs)
        # TODO: update it if dongle_prefix changes

        # Thread side server
        # TODO: bind to both RLOC and LL
        self.server_mm = CoapServer(
            addr='::',
            port=DEFS.PORT_MM,
            resources=[(URI.tuple(URI.N_MR), Res_N_MR())])
        self.server_mc = CoapServer(
            addr='::',
            port=DEFS.PORT_MC,
            resources=[(URI.tuple(URI.N_MR), Res_N_MR())])
        # TODO: bind to all_network_bbrs
        self.server_bb = CoapServer(
            addr='::',
            port=DEFS.PORT_BB,
            resources=[(URI.tuple(URI.B_BMR), Res_B_BMR())])
        time.sleep(3)
        KSH.add_mcaddr()
        # TODO: /n/dr

    def kstop(self):
        self.server_mm.stop()
        self.server_mc.stop()
        self.server_bb.stop()
        bash('smcroute -k')
        db.set('bbr_status', 'off')

    async def periodic(self):
        MCAST_HNDLR.reg_periodic()