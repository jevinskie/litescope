#!/usr/bin/env python3

# Copyright (c) 2022 Jevin Sweval <jevinsweval@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

import argparse

from liteeth.phy.model import LiteEthPHYModel
from litex.build.generic_platform import *
from litex.build.sim import SimPlatform
from litex.build.sim.config import SimConfig
from litex.soc.integration.builder import *
from litex.soc.integration.soc_core import *
from migen import *

from litescope import LiteScopeAnalyzer

# IOs ----------------------------------------------------------------------------------------------

_io = [
    ("sys_clk", 0, Pins(1)),
    ("sys_rst", 0, Pins(1)),
    (
        "eth_clocks",
        0,
        Subsignal("tx", Pins(1)),
        Subsignal("rx", Pins(1)),
    ),
    (
        "eth",
        0,
        Subsignal("source_valid", Pins(1)),
        Subsignal("source_ready", Pins(1)),
        Subsignal("source_data", Pins(8)),
        Subsignal("sink_valid", Pins(1)),
        Subsignal("sink_ready", Pins(1)),
        Subsignal("sink_data", Pins(8)),
    ),
]


# Platform -----------------------------------------------------------------------------------------


class Platform(SimPlatform):
    def __init__(self):
        SimPlatform.__init__(self, "SIM", _io)


# Bench SoC ----------------------------------------------------------------------------------------


class SimSoC(SoCCore):
    def __init__(self, sys_clk_freq=None, slim=False, **kwargs):
        platform = Platform()
        sys_clk_freq = int(sys_clk_freq)

        # SoCCore ----------------------------------------------------------------------------------
        SoCCore.__init__(
            self,
            platform,
            clk_freq=sys_clk_freq,
            ident="litescope simulation",
            **kwargs,
        )

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = CRG(platform.request("sys_clk"))

        # Etherbone --------------------------------------------------------------------------------
        if not slim:
            self.submodules.ethphy = LiteEthPHYModel(self.platform.request("eth"))
            self.add_etherbone(phy=self.ethphy, ip_address="192.168.42.50")

        # LiteScope Analyzer -----------------------------------------------------------------------
        count = Signal(24)
        self.sync += count.eq(count + 1)
        count_div4 = Signal(len(count) - 2)
        self.comb += count_div4.eq(count[2:])
        count_div16 = Signal(len(count) - 4)
        self.comb += count_div16.eq(count[4:])
        analyzer_signals = [
            # count_div4,
            # count_div16,
            self.cpu.ibus,
        ]
        # if not slim:
        #     analyzer_signals.append(self.ethphy.sink)
        self.submodules.analyzer = LiteScopeAnalyzer(
            analyzer_signals,
            depth=1024,
            rle_nbits_min=15,
            clock_domain="sys",
            samplerate=self.sys_clk_freq,
            csr_csv="analyzer.csv",
        )
        self.add_csr("analyzer")


# Main ---------------------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LiteEth Bench Simulation")
    parser.add_argument("--opt-level", default="O3", help="Verilator optimization level")
    parser.add_argument("--debug-soc-gen", action="store_true", help="Don't build the SoC, just set it up")
    parser.add_argument("--slim", action="store_true", help="Less modules to make generated verilog shorter")
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()

    sys_clk_freq = int(10e6)

    sim_config = SimConfig()
    sim_config.add_clocker("sys_clk", freq_hz=sys_clk_freq)
    sim_config.add_module("ethernet", "eth", args={"interface": "tap0", "ip": "192.168.42.100"})

    soc_kwargs = soc_core_argdict(args)
    builder_kwargs = builder_argdict(args)

    soc_kwargs["sys_clk_freq"] = sys_clk_freq
    # soc_kwargs["cpu_type"] = "None"
    soc_kwargs["uart_name"] = "crossover" if not args.slim else "stub"
    soc_kwargs["ident_version"] = True

    builder_kwargs["csr_csv"] = "csr.csv"

    soc = SimSoC(**soc_kwargs, slim=args.slim)
    if not args.debug_soc_gen:
        builder = Builder(soc, **builder_kwargs)
        for i in range(2):
            build = i == 0
            run = i == 1 and builder.compile_gateware
            if i == 1 and not builder.compile_gateware:
                break
            builder.build(
                build=build,
                run=run,
                sim_config=sim_config,
                opt_level=args.opt_level,
                verbose=False,
            )


if __name__ == "__main__":
    main()
