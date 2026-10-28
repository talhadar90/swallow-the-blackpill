# swallow-the-blackpill

Flash STM32F411 / F401 BlackPill over USB-C on **macOS**, when `dfu-util` is failing.

> **macOS only.** Made and tested on a Mac. On Linux it maybe works (same libusb), but we did not test. Windows is not supported.

## The problem

On some BlackPill boards the DFU flashing is not working. The board is showing as `STM32 BOOTLOADER`, the erase is done OK, but the write is stopping after few KB:

```
dfu-util: dfuse_download: libusb_control_transfer returned -1 (LIBUSB_ERROR_IO)
dfu-util: Error during download
```

Same thing was happening with WebDFU in Chrome, which is not using libusb at all. So it is not the tool and not the computer.

## Why

The ROM bootloader is measuring the 25 MHz crystal with the internal oscillator (HSI) and making the USB clock from this. On some chips the HSI is little bit off, so the USB clock also is little bit wrong. Small USB requests are passing, big ones (dfu-util is sending 2 KB) are breaking in the middle.

One more thing we found: when a write is breaking, and after that you ask the bootloader for status, it is writing the half-received data into the flash. So the retry is not enough, the flash is already having wrong bytes.

## What this script is doing

- writes in very small blocks (64 bytes, one USB packet)
- if a block fails, first it sends ABORT (so the broken data is thrown away), then tries again
- reads every block back after writing; if something is wrong, erases that sector and writes it again
- skips sectors which are already correct, so you can just run it again if it stopped
- at the end reads full image back once more, then starts your firmware

It is slow (around 10 minutes for 50 KB) but it is working.

## How to use

You need a Mac, Python 3 and libusb:

```
brew install libusb
```

1. Make a `.bin` file of your firmware (not `.elf`, not `.hex`).
2. Connect a wire from **A10 to G** on the BlackPill. Without it the bootloader sometimes is not showing on USB at all.
3. Connect USB-C, then hold **BOOT0**, press and release **NRST**, release **BOOT0** after 1 second.
4. Run:

```
python3 dfu_flash.py firmware.bin
```

5. When it says `verify OK` and `started the firmware`, remove the A10 wire.

## Notes

- Tested on WeAct BlackPill STM32F411CEU6 with macOS (Apple Silicon). Should work also for F401 (same sector layout, the script handles up to 512 KB).
- Starting address is always `0x08000000`.
- If it is still failing a lot, try warming the main chip a little bit (finger or hair dryer), the HSI is changing with temperature.
- Other ways that always work: ST-Link (SWD), or the serial bootloader on A9/A10 with a USB-TTL adapter.

## Thanks

The HSI explanation is from a Reddit post on r/embedded, "WeAct blackpill woes (DFU bootloader fails)". The A10 tip is from the stm32duino forum.
