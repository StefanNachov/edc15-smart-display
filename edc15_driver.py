"""
FastLifeBG — KW1281 Driver  v5
ECU: Bosch EDC15P+ (1.9 TDI 131ps) | Cable: CH340 KKL | Pi 3B

Session stability fixes confirmed working:
  - _send_block: end byte sent + echo drained only, nothing else read
  - connect(): after group 7, reads ECU's 0x09 turn-token WITHOUT responding
    (responding created infinite ping-pong loop)
  - read_groups(): after E7 ACK, reads ECU's 0x09 turn-token without responding
  - read_groups(): reset_input_buffer() + stash=None before each group request
  - Groups 2+11 read in single call per cycle (faster polling ~250ms)
  - Group 7 refreshed every 10s (IAT + fuel temp update while driving)
"""
import serial, time, logging, argparse, sys, os
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("edc15")

ATM_MBAR  = 1013.25
BLOCK_END = 0x03

@dataclass
class ECUData:
    timestamp:         float = field(default_factory=time.time)
    connected:         bool  = False
    rpm:               Optional[float] = None
    load_pct:          Optional[float] = None
    injection_qty:     Optional[float] = None
    coolant_temp:      Optional[float] = None
    intake_air_temp:   Optional[float] = None
    fuel_temp:         Optional[float] = None
    boost_mbar:        Optional[float] = None   # MAP absolute mbar  (g11 pos1)
    boost_target_mbar: Optional[float] = None   # boost target mbar  (g11 pos2)
    vnt_pct:           Optional[float] = None   # VNT vane pos %     (g11 pos3)
    cyl_balance:       Optional[list]  = None   # per-cylinder mg/stk (g13)

TYPE_CONV = {
    0x01: lambda a,b: 0.2*a*b,
    0x04: lambda a,b: abs(b-128)*0.01*a,
    0x05: lambda a,b: a*(b-100)*0.1,
    0x06: lambda a,b: 0.001*a*b,
    0x07: lambda a,b: 0.01*a*b,
    0x08: lambda a,b: 0.1*a*b,
    0x0A: lambda a,b: 0.1*a*b,
    0x0D: lambda a,b: (b-127)*0.02*a,
    0x10: lambda a,b: 0.1*a*b,
    0x11: lambda a,b: 0.1*a*b,
    0x14: lambda a,b: b*0.1,
    0x15: lambda a,b: (b-128)*0.01*a,
    0x16: lambda a,b: b*0.1,
    0x17: lambda a,b: b*a/256.0,
    0x1A: lambda a,b: b-a,
    0x21: lambda a,b: 100.0*b/a if a else 100.0*b,
    0x22: lambda a,b: (b-128)*0.01*a,
    0x23: lambda a,b: a*b/100.0,
    0x27: lambda a,b: a*b/256.0,
    0x02: lambda a,b: a*b/256.0,       # generator load %
    0x31: lambda a,b: a*b/4.0,
    0x33: lambda a,b: a*(b-128)/128.0,  # signed per-cylinder balance
}

def decode_type(t, a, b):
    fn = TYPE_CONV.get(t)
    if fn is None: return None
    try: return fn(a, b)
    except: return None


class KW1281Driver:
    ECU_ADDR = 0x01
    BAUD     = 9600
    BD       = 0.005

    def __init__(self, port, debug=False):
        self.port=port; self.debug=debug
        self.ser=None; self.counter=0; self._connected=False
        self._stash=None; self._atm_mbar=ATM_MBAR
        self._iat_cache=None; self._fuel_temp_cache=None
        self._g7_last=0.0

    def _rb(self, timeout=1.5):
        if self._stash is not None:
            b=self._stash; self._stash=None; return b
        dl=time.time()+timeout
        while time.time()<dl:
            if self.ser.in_waiting: return self.ser.read(1)[0]
            time.sleep(0.001)
        return None

    def _drain_echo(self, timeout=0.060):
        dl=time.time()+timeout
        while time.time()<dl:
            if self.ser.in_waiting>=1: self.ser.read(1); return
            time.sleep(0.001)

    def _recv_ack(self, timeout=1.5):
        b=self._rb(timeout)
        if b is None: return None
        comp=(~b)&0xFF
        time.sleep(self.BD)
        self.ser.write(bytes([comp])); self._drain_echo()
        if self.debug: log.debug(f"  RX 0x{b:02X} → ack 0x{comp:02X}")
        return b

    def _read_block(self, timeout=1.5):
        while True:
            length=self._rb(timeout)
            if length is None: return None
            if length>=3: break
            if self.debug: log.debug(f"  skip 0x{length:02X}")
        comp=(~length)&0xFF
        time.sleep(self.BD)
        self.ser.write(bytes([comp])); self.ser.flush(); self._drain_echo()
        if self.debug: log.debug(f"  RX 0x{length:02X} → ack 0x{comp:02X}")
        counter=self._recv_ack()
        if counter is None: return None
        title=self._recv_ack()
        if title is None: return None
        data=[]
        for _ in range(max(0,length-3)):
            dd=self._recv_ack(timeout=1.0)
            if dd is None: return None
            data.append(dd)
        self._rb(timeout=0.5)   # consume 0x03 end byte
        self.counter=counter
        if self.debug:
            log.debug(f"  BLOCK RX title=0x{title:02X} ctr={counter} "
                      f"data={[f'0x{x:02X}' for x in data]}")
        return {"counter":counter,"title":title,"data":data}

    def _send_block(self, title, data=[]):
        self.counter=(self.counter+1)&0xFF
        payload=[len(data)+3,self.counter,title]+list(data)
        if self.debug: log.debug(f"  BLOCK TX title=0x{title:02X} ctr={self.counter}")
        for b in payload:
            time.sleep(self.BD)
            self.ser.write(bytes([b])); self._drain_echo()
            ack=self._rb(timeout=0.5)
            if self.debug:
                exp=(~b)&0xFF
                ok="✓" if ack==exp else f"?? got 0x{ack if ack else 0:02X} exp 0x{exp:02X}"
                log.debug(f"  TX 0x{b:02X} {ok}")
        # end byte — drain echo only, never read ECU complement after this
        time.sleep(self.BD)
        self.ser.write(bytes([BLOCK_END])); self.ser.flush(); self._drain_echo()

    def _five_baud_init(self):
        log.info(f"5-baud init → ECU 0x{self.ECU_ADDR:02X}")
        bits=[0]+[(self.ECU_ADDR>>i)&1 for i in range(8)]+[1]
        for bit in bits:
            self.ser.break_condition=(bit==0); time.sleep(0.200)
        self.ser.break_condition=False
        time.sleep(0.060); self.ser.reset_input_buffer(); time.sleep(0.300)

    def connect(self):
        log.info(f"Opening {self.port}...")
        self.ser=serial.Serial(
            port=self.port,baudrate=self.BAUD,
            bytesize=serial.EIGHTBITS,parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,timeout=0)
        self.ser.reset_input_buffer(); self.counter=0; self._stash=None

        self._five_baud_init()

        log.info("Sync phase...")
        for _ in range(10):
            b=self._rb(timeout=1.5)
            if b is None: break
            if b not in (0x55,0x95,0xD5):
                self._stash=b
                log.debug(f"  F6 block starting, first byte=0x{b:02X}"); break
            kb1=self._rb(timeout=0.5); kb2=self._rb(timeout=0.5)
            if kb1 is None or kb2 is None: continue
            log.debug(f"  Sync 0x{b:02X} KB1=0x{kb1:02X} KB2=0x{kb2:02X}")
            if kb2&0x80:
                time.sleep(0.025); comp=(~kb2)&0xFF
                log.debug(f"  → 0x{comp:02X}")
                self.ser.write(bytes([comp])); self.ser.flush(); self._drain_echo()
        log.info("Sync complete ✓")

        log.info("Reading init blocks...")
        got_ident=False
        for _ in range(50):
            blk=self._read_block(timeout=3.0)
            if blk is None: log.error("Timeout reading init block"); return False
            t=blk["title"]; ctr=blk["counter"]; data=blk["data"]
            if t==0xF6:
                text=''.join(chr(d) if 32<=d<127 else '.' for d in data)
                log.info(f"  Ident: [{text.strip()}]")
                got_ident=True; self._send_block(0x09)
            elif t==0x09:
                log.info(f"  ECU ready ✓ (0x09 ctr={ctr})"); break
            elif t==0x0A:
                self._send_block(0x09)
                if got_ident:
                    log.info(f"  ECU ready ✓ (0x0A after ident ctr={ctr})"); break
            elif t==0x06:
                log.error("ECU ended session during init"); return False
            else:
                self._send_block(0x09)

        self._connected=True
        log.info("ECU connected ✓")
        self._atm_mbar=ATM_MBAR
        self._iat_cache=None; self._fuel_temp_cache=None
        self._g7_last=0.0  # force g7 read on first cycle

        # Read group 7 once at connect for IAT + fuel temp
        try:
            g7=self.read_groups([7])
            if g7 and 7 in g7 and g7[7] and len(g7[7])>=4:
                v=g7[7]
                if v[0] is not None and -40<v[0]<150: self._fuel_temp_cache=v[0]
                if v[2] is not None and -40<v[2]<150: self._iat_cache=v[2]
                log.info(f"  IAT:{self._iat_cache}°C  FuelTemp:{self._fuel_temp_cache}°C")
        except Exception as e:
            log.warning(f"  Group 7 init read failed: {e}")

        # KEY FIX: read ECU's 0x09 turn-token WITHOUT responding
        # Responding causes infinite ping-pong loop
        log.debug("  Waiting for ECU turn-token after group 7...")
        for _ in range(20):
            blk=self._read_block(timeout=0.5)
            if blk is None: break   # ECU went silent — we have the turn
            t=blk["title"]
            log.debug(f"  Post-g7 block: 0x{t:02X}")
            if t==0x09: break       # ECU handed us turn — take it, do NOT respond
            elif t==0x06: log.error("ECU ended session after g7"); return False
            else: self._send_block(0x09)
        time.sleep(0.050)
        log.debug("  Turn received — ready to poll")
        return True

    def disconnect(self):
        if self._connected and self.ser and self.ser.is_open:
            try: self._send_block(0x06)
            except: pass
        if self.ser and self.ser.is_open: self.ser.close()
        self._connected=False; log.info("Disconnected")

    def read_fault_codes(self):
        """Read DTC fault codes. Returns list of (code_int, status_byte, desc) or None."""
        if not self._connected: return None
        FAULT_STATUS={0x00:"Sporadic",0x01:"Static",0x10:"Sporadic",0x11:"Static+MIL"}
        try:
            time.sleep(0.100)
            self.ser.reset_input_buffer(); self._stash=None
            self._send_block(0x07)
            faults=[]
            for _ in range(50):
                blk=self._read_block(timeout=3.0)
                if blk is None: break
                t,data=blk["title"],blk["data"]
                if t==0xFC:
                    i=0
                    while i+2<len(data):
                        code=(data[i]<<8)|data[i+1]; st=data[i+2]
                        faults.append((code,st,FAULT_STATUS.get(st&0x11,f"0x{st:02X}")))
                        i+=3
                    self._send_block(0x09)
                elif t==0x09:
                    self._send_block(0x09); break
                elif t==0x06:
                    self._connected=False; return None
                else:
                    self._send_block(0x09)
            # drain turn-token
            for _ in range(5):
                blk=self._read_block(timeout=0.5)
                if blk is None: break
                if blk["title"]==0x09: break
                self._send_block(0x09)
            time.sleep(0.050)
            return faults
        except Exception as e:
            log.error(f"Fault code read: {e}"); return None

    def read_groups(self, groups=None):
        if not self._connected: return None
        if groups is None: groups=[2]
        results={}
        for gn in groups:
            try:
                time.sleep(0.100)
                # flush before transmit — prevents stale bytes corrupting ACK reads
                self.ser.reset_input_buffer(); self._stash=None
                self._send_block(0x29,[gn])
                db=None
                for _ in range(200):
                    blk=self._read_block(timeout=3.0)
                    if blk is None:
                        log.warning(f"  Timeout group {gn}"); return None
                    t=blk["title"]; data=blk["data"]
                    if t==0xE7:
                        db=blk; break
                    elif t==0x09:
                        self._send_block(0x09)
                    elif t==0x0A:
                        if data and data[0]==gn: self._send_block(0x29,[gn])
                        else: self._send_block(0x09)
                    elif t==0x06:
                        self._connected=False; return None
                    else:
                        self._send_block(0x09)
                if db:
                    dat=db["data"]; vals=[]; i=0
                    while i+2<len(dat):
                        vals.append(decode_type(dat[i],dat[i+1],dat[i+2])); i+=3
                    results[gn]=vals; results[f"{gn}_raw"]=list(dat)
                    log.debug(f"  Group {gn}: {vals}")
                    # ACK the E7 block
                    self._send_block(0x09)
                    # KEY FIX: read ECU's 0x09 turn-token WITHOUT responding
                    turn=self._read_block(timeout=0.5)
                    if turn and turn["title"]==0x06:
                        self._connected=False; return None
                else:
                    results[gn]=None; results[f"{gn}_raw"]=[]
            except Exception as e:
                log.error(f"  Group {gn}: {e}"); return None
        return results

    def read_ecu_data(self):
        ecu=ECUData(timestamp=time.time(),connected=self._connected)
        if not self._connected: return ecu

        # Groups 2+11 in single call — RPM/load/inj/coolant + boost/target/VNT
        g=self.read_groups([2,11])
        if g is None:
            ecu.connected=False; self._connected=False; return ecu
        ecu.connected=True

        if 2 in g and g[2] and len(g[2])>=4:
            v=g[2]
            ecu.rpm          =v[0]
            ecu.load_pct     =round(v[1],1) if v[1] is not None else None
            ecu.injection_qty=v[2]
            ecu.coolant_temp =v[3]

        if 11 in g and g[11] and len(g[11])>=4:
            v=g[11]
            ecu.boost_mbar       =v[1]
            ecu.boost_target_mbar=v[2]
            ecu.vnt_pct          =v[3]

        # Group 7: refresh every 10s — IAT + fuel temp change slowly
        now=time.time()
        if now-self._g7_last>10.0:
            g7=self.read_groups([7])
            if g7 is None:
                ecu.connected=False; self._connected=False; return ecu
            if 7 in g7 and g7[7] and len(g7[7])>=4:
                v=g7[7]
                if v[0] is not None and -40<v[0]<150: self._fuel_temp_cache=v[0]
                if v[2] is not None and -40<v[2]<150: self._iat_cache=v[2]
            self._g7_last=now
            log.debug(f"  G7 refresh: IAT={self._iat_cache}°C Fuel={self._fuel_temp_cache}°C")

        ecu.intake_air_temp=self._iat_cache
        ecu.fuel_temp      =self._fuel_temp_cache

        # Group 13: per-cylinder balance — refresh every 10s
        # Note: a=6 confirmed from live scan, scale is ±6.0mg max
        # VCDS display caps at ±2.99 but real values can exceed this
        if not hasattr(self,'_g13_last'): self._g13_last=0.0
        if not hasattr(self,'_cyl_cache'): self._cyl_cache=None
        now2=time.time()
        if now2-self._g13_last>10.0:
            g13=self.read_groups([13])
            if g13 and 13 in g13 and g13[13] and len(g13[13])>=4:
                self._cyl_cache=g13[13][:4]
                log.debug(f"  G13: {self._cyl_cache}")
            self._g13_last=now2
        ecu.cyl_balance=self._cyl_cache
        return ecu


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--port",  default="/dev/ttyUSB0")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--once",  action="store_true")
    args=p.parse_args()
    if args.debug: logging.getLogger().setLevel(logging.DEBUG)

    drv=KW1281Driver(port=args.port,debug=args.debug)
    if not drv.connect(): sys.exit(1)

    log.info("Reading — Ctrl+C to stop")
    def fmt(v,f,fb="----"):
        try: return format(v,f) if v is not None else fb
        except: return fb
    try:
        while True:
            d=drv.read_ecu_data()
            boost=((d.boost_mbar-ATM_MBAR)/1000.0) if d.boost_mbar else None
            print(f"\rRPM:{fmt(d.rpm,'.0f')} "
                  f"BOOST:{fmt(boost,'+.2f')}bar "
                  f"VNT:{fmt(d.vnt_pct,'.0f')}% "
                  f"LOAD:{fmt(d.load_pct,'.0f')}% "
                  f"IAT:{fmt(d.intake_air_temp,'.0f')}°C "
                  f"COOL:{fmt(d.coolant_temp,'.0f')}°C "
                  f"FUEL:{fmt(d.fuel_temp,'.0f')}°C "
                  f"INJ:{fmt(d.injection_qty,'.1f')}mg",
                  end="",flush=True)
            if not d.connected:
                log.warning("\nLost — reconnecting...")
                time.sleep(3); drv.disconnect()
                if not drv.connect(): break
            if args.once: print(); break
            time.sleep(0.05)
    except KeyboardInterrupt: print()
    finally: drv.disconnect()

if __name__=="__main__": main()
