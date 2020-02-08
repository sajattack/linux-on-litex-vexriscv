#!/usr/bin/env python3

import os
import subprocess

from migen import *

from litex.soc.interconnect import wishbone
from litex.soc.integration.soc_core import mem_decoder

from litex.soc.cores.spi_flash import SpiFlash
from litex.soc.cores.gpio import GPIOOut, GPIOIn
from litex.soc.cores.spi import SPIMaster
from litex.soc.cores.bitbang import I2CMaster
from litex.soc.cores.xadc import XADC
from litex.soc.cores.pwm import PWM
from litex.soc.cores.icap import ICAPBitstream

from litevideo.output import VideoOut

# Predefined values --------------------------------------------------------------------------------

video_resolutions = {
    "1920x1080_60Hz" : {
        "pix_clk"        : 148.5e6,
        "h-active"       : 1920,
        "h-blanking"     : 280,
        "h-sync"         : 44,
        "h-front-porch"  : 148,
        "v-active"       : 1080,
        "v-blanking"     : 45,
        "v-sync"         : 5,
        "v-front-porch"  : 36,
    },
    "1280x720_60Hz"  : {
        "pix_clk"        : 74.25e6,
        "h-active"       : 1280,
        "h-blanking"     : 370,
        "h-sync"         : 40,
        "h-front-porch"  : 220,
        "v-active"       : 720,
        "v-blanking"     : 30,
        "v-sync"         : 5,
        "v-front-porch"  : 20,
    },
    "640x480_75Hz"   : {
        "pix_clk"        : 31.5e6,
        "h-active"       : 640,
        "h-blanking"     : 200,
        "h-sync"         : 64,
        "h-front-porch"  : 16,
        "v-active"       : 480,
        "v-blanking"     : 20,
        "v-sync"         : 3,
        "v-front-porch"  : 1,
    }
}

# Helpers ------------------------------------------------------------------------------------------

def platform_request_all(platform, name):
    from litex.build.generic_platform import ConstraintError
    r = []
    while True:
        try:
            r += [platform.request(name, len(r))]
        except ConstraintError:
            break
    if r == []:
        raise ValueError
    return r

# SoCLinux -----------------------------------------------------------------------------------------

def SoCLinux(soc_cls, **kwargs):
    class _SoCLinux(soc_cls):
        csr_map = {**soc_cls.csr_map, **{
            "ctrl":       0,
            "uart":       2,
            "timer0":     3,
        }}
        interrupt_map = {**soc_cls.interrupt_map, **{
            "uart":       0,
            "timer0":     1,
        }}
        mem_map = {**soc_cls.mem_map, **{
            "emulator_ram": 0x20000000,
            "ethmac":       0xb0000000,
            "spiflash":     0xd0000000,
            "csr":          0xf0000000,
        }}

        def __init__(self, cpu_variant="linux", **kwargs):
            soc_cls.__init__(self,
                cpu_type       = "vexriscv",
                cpu_variant    = cpu_variant,
                uart_baudrate  = 1e6,
                max_sdram_size = 0x10000000, # Limit mapped SDRAM to 256MB for now
                **kwargs)

            # machine mode emulator ram
            self.submodules.emulator_ram = wishbone.SRAM(0x4000)
            self.register_mem("emulator_ram", self.mem_map["emulator_ram"], self.emulator_ram.bus, 0x4000)

        def add_spi_flash(self):
            # TODO: add spiflash1x support
            spiflash_pads = self.platform.request("spiflash4x")
            self.submodules.spiflash = SpiFlash(
                spiflash_pads,
                dummy=11,
                div=2,
                with_bitbang=True,
                endianness=self.cpu.endianness)
            self.spiflash.add_clk_primitive(self.platform.device)
            self.add_wb_slave(mem_decoder(self.mem_map["spiflash"]), self.spiflash.bus)
            self.add_memory_region("spiflash", self.mem_map["spiflash"], 0x1000000)
            self.add_csr("spiflash")

        def add_leds(self):
            self.submodules.leds = GPIOOut(Cat(platform_request_all(self.platform, "user_led")))
            self.add_csr("leds")

        def add_rgb_led(self):
            rgb_led_pads = self.platform.request("rgb_led", 0)
            for n in "rgb":
                setattr(self.submodules, "rgb_led_{}0".format(n), PWM(getattr(rgb_led_pads, n)))
                self.add_csr("rgb_led_{}0".format(n))

        def add_switches(self):
            self.submodules.switches = GPIOOut(Cat(platform_request_all(self.platform, "user_sw")))
            self.add_csr("switches")

        def add_spi(self, data_width, spi_clk_freq):
            spi_pads = self.platform.request("spi")
            self.add_csr("spi")
            self.submodules.spi = SPIMaster(spi_pads, data_width, self.clk_freq, spi_clk_freq)

        def add_i2c(self):
            self.submodules.i2c0 = I2CMaster(self.platform.request("i2c", 0))
            self.add_csr("i2c0")

        def add_xadc(self):
            self.submodules.xadc = XADC()
            self.add_csr("xadc")

        def add_framebuffer(self, video_settings):
            platform = self.platform
            #assert platform.device[:4] == "xc7a"
            dram_port = self.sdram.crossbar.get_port(
                mode="read",
                data_width=32,
                clock_domain="pix",
                reverse=True)
            framebuffer = VideoOut(
                device=platform.device,
                pads=platform.request("vga_out"),
                dram_port=dram_port)
            self.submodules.framebuffer = framebuffer
            self.add_csr("framebuffer")

            #framebuffer.driver.clocking.cd_pix.clk.attr.add("keep")
            #framebuffer.driver.clocking.cd_pix5x.clk.attr.add("keep")
            #platform.add_period_constraint(framebuffer.driver.clocking.cd_pix.clk, 1e9/video_settings["pix_clk"])
            #platform.add_period_constraint(framebuffer.driver.clocking.cd_pix5x.clk, 1e9/(5*video_settings["pix_clk"]))
            #platform.add_false_path_constraints(
            #    self.crg.cd_sys.clk,
            #    framebuffer.driver.clocking.cd_pix.clk,
            #    framebuffer.driver.clocking.cd_pix5x.clk)

            self.add_constant("litevideo_pix_clk", video_settings["pix_clk"])
            self.add_constant("litevideo_h_active", video_settings["h-active"])
            self.add_constant("litevideo_h_blanking", video_settings["h-blanking"])
            self.add_constant("litevideo_h_sync", video_settings["h-sync"])
            self.add_constant("litevideo_h_front_porch", video_settings["h-front-porch"])
            self.add_constant("litevideo_v_active", video_settings["v-active"])
            self.add_constant("litevideo_v_blanking", video_settings["v-blanking"])
            self.add_constant("litevideo_v_sync", video_settings["v-sync"])
            self.add_constant("litevideo_v_front_porch", video_settings["v-front-porch"])

        def add_icap_bitstream(self):
            self.submodules.icap_bit = ICAPBitstream();
            self.add_csr("icap_bit")

        def configure_ethernet(self, local_ip, remote_ip):
            local_ip = local_ip.split(".")
            remote_ip = remote_ip.split(".")

            self.add_constant("LOCALIP1", int(local_ip[0]))
            self.add_constant("LOCALIP2", int(local_ip[1]))
            self.add_constant("LOCALIP3", int(local_ip[2]))
            self.add_constant("LOCALIP4", int(local_ip[3]))

            self.add_constant("REMOTEIP1", int(remote_ip[0]))
            self.add_constant("REMOTEIP2", int(remote_ip[1]))
            self.add_constant("REMOTEIP3", int(remote_ip[2]))
            self.add_constant("REMOTEIP4", int(remote_ip[3]))

        def configure_boot(self):
            if hasattr(self, "spiflash"):
                self.add_constant("FLASH_BOOT_ADDRESS", 0x00400000)

        def generate_dts(self, board_name):
            json = os.path.join("build", board_name, "csr.json")
            dts = os.path.join("build", board_name, "{}.dts".format(board_name))
            subprocess.check_call(
                "./json2dts.py {} > {}".format(json, dts), shell=True)

        def compile_dts(self, board_name):
            dts = os.path.join("build", board_name, "{}.dts".format(board_name))
            dtb = os.path.join("buildroot", "rv32.dtb")
            subprocess.check_call(
                "dtc -O dtb -o {} {}".format(dtb, dts), shell=True)

        def compile_emulator(self, board_name):
            os.environ["BOARD"] = board_name
            subprocess.check_call("cd emulator && make", shell=True)

    return _SoCLinux(**kwargs)
