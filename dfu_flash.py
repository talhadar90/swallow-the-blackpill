"""swallow-the-blackpill: fault-tolerant USB DFU flasher for STM32F4x1 BlackPill boards (ROM bootloader, DfuSe,
0483:df11). macOS (tested); Linux untested; Windows not supported.

  python3 dfu_flash.py firmware.bin [block_bytes]      (default 64 = one USB packet per write)

Put the board in DFU first: hold BOOT0, tap NRST, release BOOT0.

Why: on some WeAct BlackPills the ROM bootloader derives the USB clock from the 25 MHz crystal measured against an
off-spec internal oscillator, so the USB clock is slightly wrong. Short requests get through, long ones (dfu-util's
2 KB writes) fail part-way and dfu-util gives up. This tool:
  - sets the address pointer before every block and writes small blocks (fewer USB packets per request),
  - after a broken transfer sends ABORT before anything else (a GETSTATUS would make the bootloader program its
    half-received buffer into flash), then retries that block (up to 20 times), re-opening the device if needed,
  - works sector by sector: a sector that already holds the image is skipped (so a run that stopped part-way just
    continues); otherwise erase, write, read every block straight back, and redo the sector if a block came back
    wrong, then reads the whole image back once more and starts the code,
  - a run that stopped part-way continues from the last good block (no new erase), a block that keeps failing is
    written in 16-, 8-, 4-byte pieces, and a stuck link gets a USB reset (like re-plugging the cable).
Uses libusb through ctypes, no Python packages needed.
"""
import ctypes as C, ctypes.util, os, sys, time


def load_libusb():
    names = [ctypes.util.find_library("usb-1.0"), "/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib",
             "libusb-1.0.so.0", "libusb-1.0.so", "libusb-1.0.dll"]
    for n in names:
        if n:
            try:
                return C.CDLL(n)
            except OSError:
                pass
    raise SystemExit("libusb not found: brew install libusb")


LIB = load_libusb()
LIB.libusb_open_device_with_vid_pid.restype = C.c_void_p
LIB.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16]
LIB.libusb_control_transfer.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint16, C.c_uint16, C.c_void_p, C.c_uint16, C.c_uint]
LIB.libusb_reset_device.argtypes = [C.c_void_p]
for f in ("libusb_claim_interface", "libusb_release_interface", "libusb_set_interface_alt_setting", "libusb_close"):
    getattr(LIB, f).argtypes = [C.c_void_p] + ([C.c_int] * (2 if f == "libusb_set_interface_alt_setting" else 1 if f != "libusb_close" else 0))

VID, PID = 0x0483, 0xDF11
DNLOAD, UPLOAD, GETSTATUS, CLRSTATUS, ABORT = 1, 2, 3, 4, 6
S_IDLE, S_DNBUSY, S_DNIDLE, S_UPIDLE, S_ERROR = 2, 4, 5, 9, 10
BASE = 0x08000000
SECTORS = [16, 16, 16, 16, 64, 128, 128, 128]            # STM32F401/F411 flash sectors, KB (up to 512 KB)
TIMEOUT_MS = 3000


class UsbError(Exception):
    pass


class Dfu:
    def __init__(self):
        self.ctx = C.c_void_p()
        if LIB.libusb_init(C.byref(self.ctx)) != 0:
            raise SystemExit("libusb_init failed")
        self.h = None
        self.open()

    def open(self, wait=10.0):
        if self.h:
            LIB.libusb_release_interface(self.h, 0)
            LIB.libusb_close(self.h)
            self.h = None
        end = time.time() + wait
        while time.time() < end:
            h = LIB.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
            if h:
                if LIB.libusb_claim_interface(h, 0) == 0 and LIB.libusb_set_interface_alt_setting(h, 0, 0) == 0:
                    self.h = h
                    return
                LIB.libusb_close(h)
            time.sleep(0.3)
        raise SystemExit("STM32 BOOTLOADER not found: put the board in DFU mode (hold BOOT0, tap NRST, release BOOT0)")

    def ctrl(self, out, req, value, data=b"", length=0):
        if out:
            buf = C.create_string_buffer(bytes(data), len(data)) if data else None
            r = LIB.libusb_control_transfer(self.h, 0x21, req, value, 0, buf, len(data), TIMEOUT_MS)
            if r < 0:
                raise UsbError(f"control OUT req {req} -> {r}")
            return b""
        buf = C.create_string_buffer(length)
        r = LIB.libusb_control_transfer(self.h, 0xA1, req, value, 0, buf, length, TIMEOUT_MS)
        if r < 0:
            raise UsbError(f"control IN req {req} -> {r}")
        return buf.raw[:r]

    def status(self):
        s = self.ctrl(False, GETSTATUS, 0, length=6)
        if len(s) != 6:
            raise UsbError("short GETSTATUS")
        return s[0], s[1] | s[2] << 8 | s[3] << 16, s[4]          # status, poll ms, state

    def wait_idle(self, want=(S_DNIDLE, S_IDLE), limit=15.0):
        end = time.time() + limit
        while time.time() < end:
            st, poll, state = self.status()
            if state == S_ERROR or st != 0:
                raise UsbError(f"DFU error status {st} state {state}")
            if state in want:
                return
            time.sleep(max(poll, 5) / 1000)
        raise UsbError("timeout waiting for the bootloader")

    def recover(self):
        # ABORT first: after a broken DNLOAD the bootloader holds a half-received buffer, and a GETSTATUS would make
        # it program that buffer into flash (seen as extra 0 bits in the block). ABORT drops the buffer unwritten.
        for _ in range(3):
            try:
                try:
                    self.ctrl(True, ABORT, 0)
                except UsbError:
                    pass
                st, _, state = self.status()
                if state == S_ERROR:
                    self.ctrl(True, CLRSTATUS, 0)
                self.ctrl(True, ABORT, 0)
                st, _, state = self.status()
                if state == S_IDLE:
                    return
            except UsbError:
                time.sleep(0.2)
        self.open()                                                # device may have re-enumerated

    def reset(self):
        """USB port reset: the bootloader re-enumerates, like unplugging and replugging the cable."""
        try:
            LIB.libusb_reset_device(self.h)
        except Exception:
            pass
        time.sleep(1.0)
        self.open()

    def cmd(self, data):                                           # DfuSe special command (set address / erase)
        self.ctrl(True, DNLOAD, 0, data)
        self.wait_idle()

    def set_addr(self, a):
        self.cmd(bytes([0x21]) + a.to_bytes(4, "little"))

    def erase(self, a):
        self.cmd(bytes([0x41]) + a.to_bytes(4, "little"))

    def write(self, a, blk):
        self.set_addr(a)
        self.ctrl(True, DNLOAD, 2, blk)                            # block 2 = at the address pointer
        self.wait_idle()

    def read(self, a, n):
        self.set_addr(a)
        self.ctrl(True, ABORT, 0)                                  # upload needs dfuIDLE
        return self.ctrl(False, UPLOAD, 2, length=n)

    def leave(self, a):
        self.set_addr(a)
        try:
            self.ctrl(True, DNLOAD, 0, b"")                        # zero-length download = leave DFU, run the code
            self.status()
        except UsbError:
            pass                                                   # the chip resets and drops off USB: expected


def retry(dfu, what, fn, tries=20):
    for t in range(1, tries + 1):
        try:
            return fn()
        except UsbError as e:
            if t == tries:
                raise SystemExit(f"{what}: gave up after {tries} tries ({e})")
            time.sleep(0.05 * t)
            if t % 5 == 0:
                dfu.reset()                                       # a stuck link: re-enumerate, then carry on
            dfu.recover()


def write_split(dfu, a, blk):
    """Write one block; if it keeps failing, write it in 16-, then 8-, then 4-byte pieces. On these boards some byte
    patterns fail every time in a 64-byte transfer (USB bit stuffing with a slightly-off USB clock); shorter transfers
    get through. Pieces already written are not touched again."""
    done = 0
    for size in (len(blk), 16, 8, 4):
        while done < len(blk):
            piece = blk[done:done + size]
            for t in range(6):
                try:
                    dfu.write(a + done, piece)
                    break
                except UsbError:
                    time.sleep(0.05 * (t + 1))
                    dfu.recover()
            else:
                break                                              # this size keeps failing: try a smaller one
            done += len(piece)
        if done == len(blk):
            return
        print(f"\r  0x{a + done:08x}: {size}-byte writes keep failing, trying smaller pieces", flush=True)
    raise SystemExit(f"write 0x{a + done:08x}: fails even in 4-byte pieces")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or not os.path.isfile(args[0]):
        raise SystemExit(__doc__)
    image = open(args[0], "rb").read()
    if len(image) > sum(SECTORS) * 1024:
        raise SystemExit("image is bigger than 512 KB")
    bs = int(args[1]) if len(args) > 1 else 64
    image += b"\xff" * (-len(image) % 4)
    dfu = Dfu()
    retry(dfu, "start", dfu.recover)
    print(f"connected to STM32 BOOTLOADER, image {len(image)} bytes, {bs}-byte blocks")

    def read_back(a, n):                                           # reads are short and reliable; retried anyway
        return b"".join(retry(dfu, f"read 0x{BASE + o:08x}", lambda o=o: dfu.read(BASE + o, min(bs, a + n - o)))
                        for o in range(a, a + n, bs))

    # one sector at a time: skip it if it already holds the image, else erase, write, read every block straight
    # back; a block that comes back wrong (a garbled USB transfer) means erase the sector and redo it
    off, k = 0, 0
    while off < len(image):
        size = SECTORS[k] * 1024
        part = image[off:off + size]
        have = read_back(off, len(part))
        if have == part:
            print(f"sector {k} (0x{BASE + off:08x}): already correct")
        else:
            # a run that stopped part-way leaves the sector correct up to some block and erased (FF) after it:
            # carry on from there without erasing, so every run adds to the last one
            start = next(i for i in range(0, len(part), bs) if have[i:i + bs] != part[i:i + bs])
            resume = all(b == 0xFF for b in have[start:])
            if resume:
                print(f"sector {k}: correct up to 0x{BASE + off + start:08x}, rest erased: continuing there")
            for attempt in range(1, 6):
                if not resume:
                    retry(dfu, f"erase sector {k}", lambda: dfu.erase(BASE + off))
                    start = 0
                resume = False
                bad = None
                for o in range(off + start, off + len(part), bs):
                    blk = image[o:o + bs]
                    write_split(dfu, BASE + o, blk)
                    if read_back(o, len(blk)) != blk:
                        bad = o
                        break
                    print(f"\rsector {k}: {o + len(blk) - off:6d} / {len(part)}", end="", flush=True)
                if bad is None:
                    print(f"\rsector {k} (0x{BASE + off:08x}): written and verified ({len(part)} bytes)")
                    break
                print(f"\rsector {k}: block 0x{BASE + bad:08x} came back wrong, erasing the sector again (try {attempt})")
            else:
                raise SystemExit(f"sector {k} would not program after 5 tries")
        off += size
        k += 1

    print("final check of the whole image ...", flush=True)
    if read_back(0, len(image)) != image:
        raise SystemExit("final verify FAILED")
    print("verify OK")
    dfu.recover()
    dfu.leave(BASE)
    print("started the firmware")


if __name__ == "__main__":
    main()
