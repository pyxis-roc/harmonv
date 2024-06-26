#!/usr/bin/env python3
#
# disassembler.py
#
# Routines for disassembling SASS
#
# Uses cuobjdump and/or nvdisasm (which must be in path).
#
# Author: Sreepathi Pai
# Author: Benjamin Valpey
#
# Copyright (C) 2020, 2024 University of Rochester
#
# SPDX-FileCopyrightText: 2020-2024 University of Rochester
#
# SPDX-License-Identifier: MIT

# fmt: off

from harmonv import nvfatbin
import struct
import subprocess
import tempfile
import os
import logging
import subprocess
import re
from collections import namedtuple, defaultdict
from harmonv import disasm_parser

logger = logging.getLogger(__name__)

CUOBJDUMP_RE_FUNC_START = re.compile(r'\s+Function : (?P<function>[^\s]*)$')
CUOBJDUMP_RE_FUNC_END = re.compile(r'\s+\.+$')

# four forms: the first is the scheduling info, second is a standard opcode, third indicates start of vliw group, fourth indicates end of vliw group
#                                                                /* 0x001c4400e22007f6 */
#         /*0008*/                   MOV R1, c[0x0][0x20];       /* 0x4c98078000870001 */
#         /*00a8*/         {         MOV R4, c[0x0][0x148];      /* 0x4c98078005270004 */
#         /*00b0*/                   STG.E [R2], R0;        }    /* 0xeedc200000070200 */


CUOBJDUMP_SASS_FMT = re.compile(r'''( (?# Instruction text group)
                                \s+/\*(?P<loc>[0-9A-Fa-f]+)\*/ (?# address, e.g. 0008 for the first instruction) \s+
                                (?P<startbrace>{)? (?# VLIW Start) \s+
                                (?P<text>.*); (?# The instruction line)
                                \s*(?P<endbrace>})? (?# VLIW End)
                                )? (?# End instruction text group) \s+/\*\s+
                                (?P<opcode>0x[0-9A-Fa-f]+) (?# Opcode in hexadecimal) \s+\*/$
                                ''',
                                re.VERBOSE)

BRANCH_LABEL_INFO = namedtuple('BRANCH_LABEL_INFO', 'target_label opcode')
SASS_INSN_CUOBJDUMP = namedtuple('SASS_INSN_CUOBJDUMP', 'loc opcode text raw vliw_start vliw_end')
SASS_DIRECTIVE = re.compile(r'\s*\..*$')

NVDISASM_RE_FUNC_ENTRY = re.compile(r'''
                                    \s+\.type\s+(?P<function>.*),@function$
                                    ''',
                                    re.VERBOSE)

NVDISASM_BRANCH_LBL = re.compile(r'\.L_(x_)?\d+(?=:$)')
# CAL header goes like:
#        .weak           identifier
#        .type           identifier,@function
#        .size           identifier,(.L_### - identifier)
NVDISASM_CAL_HEADER_BEGIN = re.compile(r"\s+\.weak\s+(?P<cal_name>.+)$")
NVDISASM_SASS_FMT = re.compile(r'''
                               ( (?# Instruction text group)
                                    \s+/\*(?P<loc>[0-9A-Fa-f]+)\*/ (?# address, e.g. 0008 for the first instruction) \s+
                                    (?P<text>.*?) (?# The instruction line)
                                        (   (?# Capture group for branch labels, e.g. (*"BRANCH_TARGETS .L_1"*) 
                                            (?# CUDA < 11.0 has form "TARGET= .L_\d+" and cuda >= 11.0 has form "BRANCH_TARGETS .L_\d+")
                                            \(\*"(BRANCH_TARGETS|TARGET=)\s+ 
                                            (?P<branch_target>\.L_(x_)?\d+) (?# The actual branch label matches .L_x?_?\d+)
                                            \s*"\*\)\s*
                                        )?
                                    ;  (?# Always have ';' with -novliw flag)
                                )? (?# End instruction text group) \s+/\*\s+
                                (?P<opcode>0x[0-9A-Fa-f]+) (?# Hexadecimal opcode) \s+\*/$
                                ''',
                                re.VERBOSE)

class SASSFunction(object):
    def __init__(self, function, sass_disassembly, producer, headers = None, sass_binary = None):
        self.function = function
        self.disassembly = sass_disassembly # list of SASS_*_INSN
        self.producer = producer

        self.headers = headers # list of strings
        self.binary = sass_binary
        self.arg_info = None
        self.fn_info = None
        self.cubin_info = {}
        self.constants = None
        self.relocations = None
        self.sym_info = []
        self.sharedmem = None
        self.global_init_offsets = None
        self.global_offsets = None
        self.global_init_data = None
        self.numbar = None
        self.numregs = None
        self.regcount = None
        self.frame_size = None
        self.max_stack_size = None
        self.min_stack_size = None
        self.branch_targets = None

    def __str__(self):
        return f"SASSFunction(function={repr(self.function)})"

    def set_arg_info(self, args):
        self.arg_info = args

    def set_fn_info(self, fninfo):
        self.fn_info = fninfo

    def set_syminfo(self, syminfo):
        if syminfo.other_raw & nvfatbin.STO_CUDA_ENTRY:
            self.sym_info.append('STO_CUDA_ENTRY')

    def set_relocations(self, relocations):
        out = []
        for r, sym in relocations:
            out.append({'offset': r['r_offset'],
                        'symbol': sym,
                        'info': r['r_info'],
                        'info_type': r['r_info_type']})

        self.relocations = out

    def set_constants(self, constants, update = False):
        if update and self.constants is not None:
            self.constants.extend(constants)
        else:
            self.constants = constants

    def set_sharedmem(self, shmem_size):
        self.sharedmem = shmem_size

    def set_numbar(self, numbar):
        self.numbar = numbar

    def set_numregs(self, numregs):
        self.numregs = numregs

    def set_regcount(self, regcount):
        self.numregs = regcount

    def set_frame_size(self, frame_size):
        self.frame_size = frame_size

    def set_max_stack_size(self, sz):
        self.max_stack_size = sz

    def set_min_stack_size(self, sz):
        self.min_stack_size = sz

    def set_global_init_data(self, gdata):
        self.global_init_data = gdata

    def set_global_init_offsets(self, goffsets):
        self.global_init_offsets = goffsets

    def set_global_offsets(self, goffsets):
        self.global_offsets = goffsets

    def set_branch_targets(self, branch_targets):
        self.branch_targets = branch_targets

    def to_dict(self):
        out = {'function': self.function,
               'producer': self.producer,
               'headers': self.headers,
               'binary': self.binary,
               'cubin_info': self.cubin_info,
               'disassembly': [dict(x._asdict()) for x in self.disassembly]}

        if self.arg_info:
            out['arg_info'] = [dict(x._asdict()) for x in self.arg_info]

        if self.fn_info:
            out['fn_info'] = self.fn_info

        if self.constants:
            out['constants'] = self.constants

        if self.relocations:
            out['relocations'] = self.relocations

        if self.sym_info:
            out['sym_info'] = self.sym_info

        if self.sharedmem is not None:
            out['sharedmem'] = self.sharedmem

        if self.numbar is not None:
            out['numbar'] = self.numbar

        if self.numregs is not None:
            out['numregs'] = self.numregs

        if self.regcount is not None:
            out['regcount'] = self.regcount

        if self.frame_size is not None:
            out['frame_size'] = self.frame_size

        if self.min_stack_size is not None:
            out['min_stack_size'] = self.min_stack_size

        if self.max_stack_size is not None:
            out['max_stack_size'] = self.max_stack_size

        if self.global_init_data is not None:
            # NOTE: This will leave Yaml anchors / aliases in the output
            out['global_init_data'] = self.global_init_data

        if self.global_init_offsets is not None:
            # NOTE: This will leave Yaml anchors / aliases in the output
            out['global_init_offsets'] = self.global_init_offsets

        if self.global_offsets is not None:
            out['global_offsets'] = self.global_offsets

        if self.branch_targets is not None:
            out['branch_targets'] = self.branch_targets
        else:
            out['branch_targets'] = {}

        return out


class DisassemblerCUObjdump(object):
    @staticmethod
    def _parse_cuobjdump_output(src, output):
        """Get per-function SASS dumps."""
        out = {}
        fn = None
        fn_name = None
        for lno, l in enumerate(output.splitlines(), 1):
            m = CUOBJDUMP_RE_FUNC_START.match(l) # don't have to do this on every line
            if m is not None:
                assert fn_name is None, f"{lno}: Previous function {fn_name} did not end properly"
                fn_name = m.group('function')
                fn = []
            else:
                if CUOBJDUMP_RE_FUNC_END.match(l):
                    assert fn_name is not None, f"{lno}: End-of-function marker found when no function active"
                    out[fn_name] = fn
                    fn_name = None
                    fn = None
                else:
                    if fn_name:
                        fn.append(l)
                    else:
                        # debug here for lines that 'slip through'
                        #print(l)
                        pass

        return out

    @staticmethod
    def _parse_fn_sass(fn_output):
        out = {}

        for fn, data in fn_output.items():
            header = []
            disasm = []

            # data consists of header lines, followed by disassembly
            for lno, l in enumerate(data, 1):
                m = CUOBJDUMP_SASS_FMT.match(l)
                if not m:
                    assert len(disasm) == 0, f"{lno}: Line '{l}' in middle of disassembly does not match SASS disassembly regular expression"
                    header.append(l)
                    continue
                else:
                    insn = SASS_INSN_CUOBJDUMP(loc=m.group('loc'),
                                               opcode=m.group('opcode'),
                                               text=m.group('text'),
                                               vliw_start=m.group('startbrace') is not None,
                                               vliw_end=m.group('endbrace') is not None,
                                               raw=l)
                    disasm.append(insn)

            out[fn] = (header, disasm)

        return out

    @staticmethod
    def _parse_fn_sass_2(src, fn_output):
        out = {}

        parser = disasm_parser.DisassemblyParser(src)

        for fn, data in fn_output.items():
            header = []
            disasm = []

            # data consists of header lines, followed by disassembly
            for lno, l in enumerate(data, 1):
                m = SASS_DIRECTIVE.match(l)
                if m:
                    assert len(disasm) == 0, f"{src}:{lno}: Line '{l}' in middle of disassembly looks like a directive, was expecting disassembly"
                    header.append(l)
                    continue
                else:
                    break


            disasm_text  = '\n'.join(data[lno:])
            disasm = parser.parse(disasm_text)

            out[fn] = (header, disasm)

        return out
    
    @staticmethod
    def _get_nvdisasm_bra_targets(src, nvds_output, fn_output):
        # Get mangled names of functions to isolate
        fns = set(fn_output.keys())

        status = 'Entry'
        active_fn = None

        branch_label_dict = {}
        label_targets = {}
        branch_targets = defaultdict(dict)
        first_instr_found = False
        awaiting_cal_name = None
        nvds_output += '\n' # Ensure last line is empty

        for lno, l in enumerate(nvds_output.splitlines(), 1):
            if status == 'Start' and l == f'{active_fn}:':
                status = 'End'
                continue
            elif status == 'Entry':
                m = NVDISASM_RE_FUNC_ENTRY.match(l)
                if m is None or m.group('function') not in fns:
                    last_line_label = None
                    continue
                active_fn = m.group('function')
                status = 'Start'
            
            elif status == 'End' and l == '' and last_line_label is not None:
                    # Function end is always a label followed by an empty line
                for insn in filter(lambda x: x.loc in branch_label_dict, fn_output[active_fn][1]): 
                    assert insn.opcode == branch_label_dict[insn.loc].opcode, f"{lno}: Branch label {insn.loc} does not match opcode {insn.opcode}"
                    branch_targets[active_fn][insn.loc] = label_targets.get(branch_label_dict[insn.loc].target_label)
                active_fn = None
                status = 'Entry'
                first_instr_found = False
                branch_label_dict = {}
                label_targets = {}
                last_line_label = None
            elif status == 'End' and (lbl_match := NVDISASM_BRANCH_LBL.match(l)):
                last_line_label = lbl_match.group(0)
                continue
            elif status == 'End' and (cal_match := NVDISASM_CAL_HEADER_BEGIN.match(l)) is not None:
                awaiting_cal_name = cal_match.group('cal_name')
                status = 'CalEntry'
                last_line_label = None
            elif status == 'CalEntry':
                if l == f'{awaiting_cal_name}:':
                    awaiting_cal_name = None
                    status = 'End'
                else:
                    assert awaiting_cal_name in l, f"{lno}: Line '{l}' in middle of disassembly does not match regex."
                last_line_label = None
            elif status == 'End':
                m = NVDISASM_SASS_FMT.match(l)
                if first_instr_found:
                    assert m is not None, f"{lno}: Line in middle of disassembly does not match regex.\n\t{l}"
                else:
                    first_instr_found = m is not None and m.group('loc') is not None
                if m is not None:
                    if last_line_label is not None and m.group('loc') is not None:
                        label_targets[last_line_label] = m.group('loc')
                        last_line_label = None
                    elif last_line_label is not None:
                        logger.info(f"Line '{l}' labeled by {last_line_label} has no pc. Label will point to the following line")
                    if m.group('branch_target') is not None:
                        branch_label_dict[m.group('loc')] = BRANCH_LABEL_INFO(m.group('branch_target'), m.group('opcode'))

        return branch_targets

    @staticmethod
    def disassemble(cubin, function_names = None, function_index = None, _keep = False, src = '<unknown>', add_branch_targets=False):
        function_names = [] if function_names is None else function_names
        cubin_data = cubin.get_data()
        fnargs = cubin.get_args()
        fninfo = cubin.get_fn_info()
        const = cubin.constants
        syminfo = dict([(st.name, st) for st in cubin.nvglobals])

        cubin_info = {'arch': cubin.arch}

        assert not (len(function_names) and (function_index is not None)), f"Can't specify both function_names and function_index at the same time"

        args = []
        nvds_args = []
        if function_names is not None and len(function_names):
            args.append('-fun')
            args.append(",".join(function_names))
        elif function_index is not None:
            nvds_args.append('-findex')
            nvds_args.append(str(function_index))
            args.extend(nvds_args)
        out = {}
        with tempfile.NamedTemporaryFile(suffix=".cubin", delete=False) as f:
            f.write(cubin_data)
            tmpcubin = f.name
        try:
            output = subprocess.check_output(['cuobjdump'] + args + ['-sass', tmpcubin]).decode('ascii')
            by_function = DisassemblerCUObjdump._parse_cuobjdump_output(src, output)
            fn_headers_sass = DisassemblerCUObjdump._parse_fn_sass_2(src, by_function)
            if add_branch_targets:
                nvds_output = subprocess.check_output(['nvdisasm'] + nvds_args + ['-c', '-hex', '-novliw', tmpcubin]).decode('ascii')
                fn_branch_dests = DisassemblerCUObjdump._get_nvdisasm_bra_targets(src, nvds_output, fn_headers_sass)
            for fn, (hdr, sass) in fn_headers_sass.items():
                out[fn] = SASSFunction(fn, sass_disassembly=sass, producer='cuobjdump', headers=hdr)
                if add_branch_targets and fn in fn_branch_dests: out[fn].set_branch_targets(fn_branch_dests[fn])
                if fn in fnargs: out[fn].set_arg_info(fnargs[fn])
                if fn in fninfo: out[fn].set_fn_info(fninfo[fn])
                if fn in const: out[fn].set_constants(const[fn])
                if fn in syminfo: out[fn].set_syminfo(syminfo[fn])
                if '' in const: out[fn].set_constants(const[''], update=True)
                if fn in cubin.sharedmem:
                    out[fn].set_sharedmem(cubin.sharedmem[fn])
                if f'.text.{fn}' in cubin.relocations:
                    out[fn].set_relocations(cubin.relocations[f'.text.{fn}'])
                if fn in cubin.numbar:
                    out[fn].set_numbar(cubin.numbar[fn])
                if fn in cubin.numregs:
                    out[fn].set_numregs(cubin.numregs[fn])
                if fn in cubin.regcount:
                    out[fn].set_regcount(cubin.regcount[fn])
                if fn in cubin.frame_size:
                    out[fn].set_frame_size(cubin.frame_size[fn])
                if fn in cubin.max_stack_size:
                    out[fn].set_max_stack_size(cubin.max_stack_size[fn])
                if fn in cubin.min_stack_size:
                    out[fn].set_min_stack_size(cubin.min_stack_size[fn])
                out[fn].cubin_info = cubin_info
                out[fn].set_global_init_data(cubin.global_init_data)
                out[fn].set_global_init_offsets(cubin.global_init_symbol_offset)
                out[fn].set_global_offsets(cubin.global_symbol_offset)
        except subprocess.CalledProcessError as e:
            logger.error(f'ERROR: cuobjdump failed to handle cubin (arch={cubin.arch}): {e}')
            return out
        except:
            raise
        finally:
            os.unlink(tmpcubin)

        return out

