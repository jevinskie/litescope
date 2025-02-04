#
# This file is part of LiteScope.
#
# Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2018 bunnie <bunnie@kosagi.com>
# Copyright (c) 2016 Tim 'mithro' Ansell <mithro@mithis.com>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.genlib.misc import WaitTimer
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.build.tools import write_to_file

from litex.soc.interconnect.csr import *
from litex.soc.cores.gpio import GPIOInOut
from litex.soc.interconnect import stream

from litex.gen.fhdl.sim import Monitor, MonitorArg, MonitorFSMState
from litex.gen.fhdl.utils import get_signals

# LiteScope IO -------------------------------------------------------------------------------------

class LiteScopeIO(Module, AutoCSR):
    def __init__(self, data_width):
        self.data_width = data_width
        self.input  = Signal(data_width)
        self.output = Signal(data_width)

        # # #

        self.submodules.gpio = GPIOInOut(self.input, self.output)

    def get_csrs(self):
        return self.gpio.get_csrs()

# LiteScope Analyzer -------------------------------------------------------------------------------

def core_layout(data_width):
    return [("data", data_width), ("hit", 1)]


class _Trigger(Module, AutoCSR):
    def __init__(self, data_width, depth=16):
        self.sink   = sink   = stream.Endpoint(core_layout(data_width))
        self.source = source = stream.Endpoint(core_layout(data_width))

        self.enable = CSRStorage()
        self.done   = CSRStatus()

        self.mem_write = CSR()
        self.mem_mask  = CSRStorage(data_width)
        self.mem_value = CSRStorage(data_width)
        self.mem_full  = CSRStatus()

        # # #

        # Control re-synchronization
        enable   = Signal()
        enable_d = Signal()
        self.specials += MultiReg(self.enable.storage, enable, "scope")
        self.sync.scope += enable_d.eq(enable)

        # Status re-synchronization
        done = Signal()
        self.specials += MultiReg(done, self.done.status)

        # Memory and configuration
        mem = stream.AsyncFIFO([("mask", data_width), ("value", data_width)], depth)
        mem = ClockDomainsRenamer({"write": "sys", "read": "scope"})(mem)
        self.submodules += mem
        self.comb += [
            mem.sink.valid.eq(self.mem_write.re),
            mem.sink.mask.eq(self.mem_mask.storage),
            mem.sink.value.eq(self.mem_value.storage),
            self.mem_full.status.eq(~mem.sink.ready)
        ]

        # Hit and memory read/flush
        hit   = Signal()
        flush = WaitTimer(2*depth)
        flush = ClockDomainsRenamer("scope")(flush)
        self.submodules += flush
        self.comb += [
            flush.wait.eq(~(~enable & enable_d)), # flush when disabling
            hit.eq((sink.data & mem.source.mask) == (mem.source.value & mem.source.mask)),
            mem.source.ready.eq((enable & hit) | ~flush.done),
        ]

        # Output
        self.comb += [
            sink.connect(source),
            # Done when all triggers have been consumed
            done.eq(~mem.source.valid),
            source.hit.eq(done)
        ]

class _SubSampler(Module, AutoCSR):
    def __init__(self, data_width, counter_bits=16):
        self.sink   = sink   = stream.Endpoint(core_layout(data_width))
        self.source = source = stream.Endpoint(core_layout(data_width))

        self.value = CSRStorage(counter_bits)

        # # #

        value = Signal(counter_bits)
        self.specials += MultiReg(self.value.storage, value, "scope")

        counter = Signal(counter_bits)
        done    = Signal()
        self.sync.scope += \
            If(source.ready,
                If(done,
                    counter.eq(0)
                ).Elif(sink.valid,
                    counter.eq(counter + 1)
                )
            )

        self.comb += [
            done.eq(counter == value),
            sink.connect(source, omit={"valid"}),
            source.valid.eq(sink.valid & done)
        ]


class _Mux(Module, AutoCSR):
    def __init__(self, data_width, n):
        self.sinks  = sinks  = [stream.Endpoint(core_layout(data_width)) for i in range(n)]
        self.source = source = stream.Endpoint(core_layout(data_width))

        self.value = CSRStorage(bits_for(n))

        # # #

        value = Signal(bits_for(n))
        self.specials += MultiReg(self.value.storage, value, "scope")

        cases = {}
        for i in range(n):
            cases[i] = sinks[i].connect(source)
        self.comb += Case(value, cases)


class _RunLengthEncoder(Module):
    def __init__(self, reinit, data_width):
        self.sink = sink = stream.Endpoint(core_layout(data_width))
        self.source = source = stream.Endpoint(core_layout(data_width + 1))

        valid, valid_d = sink.valid, Signal()
        self.sync.scope += valid_d.eq(valid)

        output = source.payload.data
        rle_valid = Signal()
        reinit_d = Signal()
        self.sync.scope += reinit_d.eq(reinit)

        data = sink.payload.data
        data_d = Signal(data_width)
        self.sync.scope += If(sink.valid, data_d.eq(data))

        hit, hit_d = sink.hit, Signal()
        self.sync.scope += hit_d.eq(hit)

        same, same_d = Signal(), Signal()
        self.comb += same.eq(data_d == data)
        self.sync.scope += same_d.eq(same)

        rle_data = Signal(data_width)
        rle_encoded = Signal()
        rle_last = Signal()
        self.comb += rle_last.eq(~same & same_d)

        # Keep counter size down, 24 bits is enough for 15 seconds @ 1 GHz
        self.counter_width = counter_width = min(24, data_width)
        rle_cnt_max = 2**counter_width - 1
        rle_cnt = Signal(counter_width)
        rle_ovf = Signal()

        self.submodules.fsm = fsm = FSM(reset_state="NEW")
        fsm.act("NEW",
            rle_data.eq(data_d),
            rle_encoded.eq(0),
            rle_valid.eq(valid_d),
            NextValue(rle_cnt, 0),
            NextValue(rle_ovf, 0),
            If(same & source.ready & hit_d, NextState("SAME")),
        )
        fsm.act("SAME",
            rle_data.eq(rle_cnt),
            rle_encoded.eq(1),
            rle_valid.eq(rle_last | rle_ovf),
            NextValue(rle_cnt, rle_cnt + 1),
            If(rle_cnt == rle_cnt_max,
                rle_valid.eq(1),
                NextValue(rle_ovf, 1)
            ),
            If(rle_ovf | reinit,
                rle_data.eq(data_d),
                rle_encoded.eq(0),
                NextValue(rle_cnt, 0),
                NextValue(rle_ovf, 0),
            ),
            If(rle_last, NextState("NEW")),
        )

        self.comb += [
            sink.connect(source, omit=["data", "valid", "hit"]),
            source.valid.eq(rle_valid),
            output[1:].eq(rle_data),
            output[0].eq(rle_encoded),
            source.hit.eq(hit_d)
        ]


class _Storage(Module, AutoCSR):
    def __init__(self, data_width, depth, rle=False):
        self.sink = sink = stream.Endpoint(core_layout(data_width))
        rle_reinit = Signal()
        if rle:
            self.submodules.rle = _RunLengthEncoder(rle_reinit, data_width)
            data_width += 1
            self.comb += sink.connect(self.rle.sink)
            sink_internal = self.rle.source
        else:
            sink_internal = sink

        self.enable    = CSRStorage()
        self.done      = CSRStatus()

        self.length    = CSRStorage(bits_for(depth))
        self.offset    = CSRStorage(bits_for(depth))

        read_width = min(32, data_width)
        self.mem_level = CSRStatus(bits_for(depth))
        self.mem_data  = CSRStatus(read_width)

        # # #

        # Control re-synchronization
        enable   = Signal()
        enable_d = Signal()
        self.specials += MultiReg(self.enable.storage, enable, "scope")
        self.sync.scope += enable_d.eq(enable)

        length = Signal().like(self.length.storage)
        offset = Signal().like(self.offset.storage)
        self.specials += MultiReg(self.length.storage, length, "scope")
        self.specials += MultiReg(self.offset.storage, offset, "scope")

        # Status re-synchronization
        done  = Signal()
        level = Signal().like(self.mem_level.status)
        self.specials += MultiReg(done, self.done.status)
        self.specials += MultiReg(level, self.mem_level.status)

        # Memory
        mem = stream.SyncFIFO([("data", data_width)], depth, buffered=True)
        mem = ClockDomainsRenamer("scope")(mem)
        cdc = stream.AsyncFIFO([("data", data_width)], 4)
        cdc = ClockDomainsRenamer(
            {"write": "scope", "read": "sys"})(cdc)
        self.submodules += mem, cdc

        self.comb += level.eq(mem.level)

        # Flush
        mem_flush = WaitTimer(depth)
        mem_flush = ClockDomainsRenamer("scope")(mem_flush)
        self.submodules += mem_flush

        # FSM
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("scope")(fsm)
        self.submodules += fsm
        fsm.act("IDLE",
            done.eq(1),
            If(enable & ~enable_d,
                NextState("FLUSH")
            ),
            mem.source.connect(cdc.sink)
        )
        fsm.act("FLUSH",
            mem_flush.wait.eq(1),
            mem.source.ready.eq(1),
            If(mem_flush.done,
                NextState("WAIT")
            )
        )
        fsm.act("WAIT",
            sink_internal.connect(mem.sink, omit={"hit"}),
            If(sink_internal.valid & sink_internal.hit,
                NextState("RUN"),
                NextValue(rle_reinit, 1),
            ),
            mem.source.ready.eq(mem.level >= offset)
        )
        fsm.act("RUN",
            sink_internal.connect(mem.sink, omit={"hit"}),
            NextValue(rle_reinit, 0),
            If(mem.level >= length,
                NextState("IDLE"),
            )
        )

        # Memory read
        read_source = stream.Endpoint([("data", data_width)])
        if data_width > read_width:
            pad_bits = - data_width % read_width
            w_conv = stream.Converter(data_width + pad_bits, read_width)
            self.submodules += w_conv
            self.comb += cdc.source.connect(w_conv.sink)
            self.comb += w_conv.source.connect(read_source)
        else:
            self.comb += cdc.source.connect(read_source)

        self.comb += [
            read_source.ready.eq(self.mem_data.we | ~self.enable.storage),
            self.mem_data.status.eq(read_source.data)
        ]


class LiteScopeAnalyzer(Module, AutoCSR):
    def __init__(self, groups, depth, rle_nbits_min=None, samplerate=1e12, clock_domain="sys", trigger_depth=16, subsampler_bits=16, register=False, csr_csv="analyzer.csv"):
        self.groups          = groups = self.format_groups(groups)
        self.depth           = depth
        self.samplerate      = int(samplerate)
        self.subsampler_bits = subsampler_bits
        self.rle_nbits_min   = rle_nbits_min

        self.data_width = data_width = max([sum([len(s) for s in g]) for g in groups.values()])
        if rle_nbits_min:
            self.data_width = data_width = max(data_width, rle_nbits_min)

        self.csr_csv = csr_csv

        # # #

        # Create scope clock domain
        self.clock_domains.cd_scope = ClockDomain()
        self.comb += self.cd_scope.clk.eq(ClockSignal(clock_domain))

        # Mux
        self.submodules.mux = _Mux(data_width, len(groups))
        sd = getattr(self.sync, clock_domain)
        for i, signals in groups.items():
            s = Cat(signals)
            if register:
                s_d = Signal(len(s))
                sd += s_d.eq(s)
                s = s_d
            self.comb += [
                self.mux.sinks[i].valid.eq(1),
                self.mux.sinks[i].data.eq(s)
            ]

        # Frontend
        self.submodules.trigger = _Trigger(data_width, depth=trigger_depth)
        self.submodules.subsampler = _SubSampler(data_width, counter_bits=self.subsampler_bits)

        # Storage
        self.submodules.storage = _Storage(data_width, depth, rle=bool(rle_nbits_min))

        # Pipeline
        self.submodules.pipeline = stream.Pipeline(
            self.mux.source,
            self.trigger,
            self.subsampler,
            self.storage.sink)

    def format_groups(self, groups):
        if not isinstance(groups, dict):
            groups = {0 : groups}
        new_groups = {}
        for n, signals in groups.items():
            if not isinstance(signals, list):
                signals = [signals]

            split_signals = []
            for s in signals:
                if isinstance(s, Record):
                    split_signals.extend(s.flatten())
                elif isinstance(s, FSM):
                    s.do_finalize()
                    s.finalized = True
                    split_signals.append(s.state)
                else:
                    split_signals.append(s)
            split_signals = list(dict.fromkeys(split_signals)) # Remove duplicates.
            new_groups[n] = split_signals
        return new_groups

    def export_csv(self, vns, filename):
        def format_line(*args):
            return ",".join(args) + "\n"
        r = format_line("config", "None", "data_width", str(self.data_width))
        r += format_line("config", "None", "depth", str(self.depth))
        r += format_line("config", "None", "samplerate", str(self.samplerate))
        r += format_line("config", "None", "subsampler_counter_bits", str(self.subsampler_bits))
        r += format_line("config", "None", "rle", str(int(bool(self.rle_nbits_min))))
        for i, signals in self.groups.items():
            for s in signals:
                r += format_line("signal", str(i), vns.get_name(s), str(len(s)))
        write_to_file(filename, r)

    def do_exit(self, vns):
        if self.csr_csv is not None:
            self.export_csv(vns, self.csr_csv)
