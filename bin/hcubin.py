#!/usr/bin/env python3
#
# nvcubin.py
#
# Parser for NVIDIA cubin
#
# Author: Sreepathi Pai
#
# Copyright (C) 2026, The University of Rochester
#
# SPDX-FileCopyrightText: 2026 University of Rochester
#
# SPDX-License-Identifier: MIT
# fmt: off

import argparse
from elftools.elf.elffile import ELFFile
import elftools.elf.sections
import elftools.common.exceptions
import elftools
import harmonv.nvfatbin as nvfatbin
import struct
import json
import subprocess
import shutil

def cufilt(names):
    cufilt = shutil.which('cu++filt')
    if cufilt is None:
        print("ERROR: cu++filt not found.")
        return None

    with subprocess.Popen([cufilt],
                          stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          text=True,
                          encoding='utf-8'
                          ) as p:
        outdata = []
        for n in names:
            stdout_data, stderr_data = p.communicate(n + '\n')
            outdata.append(stdout_data)

        return dict([kv for kv in zip(names, "".join(outdata).split("\n"))])

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Parse a CUBIN file"
    )

    p.add_argument("cubinfile")
    p.add_argument("-d", dest="debug", action="store_true")
    p.add_argument("-o", dest="output", help="Output JSON for function information")
    p.add_argument("--demangle", action="store_true", help="Store demangled name")

    args = p.parse_args()

    nvfatbin.DEBUG = args.debug
    nvfatbin.LIBRARY_MODE = 0

    cubin = nvfatbin.NVCubin.from_elf(args.cubinfile)
    out = {}
    demangled = {}
    for p in cubin.parts:
        if isinstance(p, nvfatbin.NVCubinPartELF):
            p.parse()
            out.update(p.fn_info)
            if args.demangle:
                demangled = cufilt(p.fn_info.keys())

            for fn in p.fn_info:
                out[fn]['EIATTR_KPARAM_INFO'] = [x._asdict() for x in p.args[fn]]
                out[fn]['EIATTR_REGCOUNT'] = p.regcount[fn]
                if fn in demangled:
                    out[fn]['demangled'] = demangled[fn]

    if args.output:
        with open(args.output, "w") as f:
            f.write(json.dumps(out, indent=' '))
    else:
        print(json.dumps(out, indent=' '))
