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

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Parse a CUBIN file"
    )

    p.add_argument("cubinfile")
    p.add_argument("-d", dest="debug", action="store_true")
    p.add_argument("-o", dest="output", help="Output JSON for function information")

    args = p.parse_args()

    nvfatbin.DEBUG = 1
    nvfatbin.LIBRARY_MODE = 0

    cubin = nvfatbin.NVCubin.from_elf(args.cubinfile)
    out = {}
    for p in cubin.parts:
        if isinstance(p, nvfatbin.NVCubinPartELF):
            p.parse()
            out.update(p.fn_info)

    if args.output:
        with open(args.output, "w") as f:
            f.write(json.dumps(out, indent=' '))
    else:
        print(json.dumps(out, indent=' '))
