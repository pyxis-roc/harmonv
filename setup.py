# SPDX-FileCopyrightText: 2019,2021 University of Rochester
#
# SPDX-License-Identifier: MIT

from setuptools import setup
from setuptools.command.install import install
from setuptools.command.develop import develop

def _run_make_ptx():
    from subprocess import check_call

    check_call(['make', '-C', 'harmonv/ptx'])


class CustomInstall(install):
    def run(self):
        _run_make_ptx()
        install.run(self)

class CustomDevelop(develop):
    def run(self):
        _run_make_ptx()
        develop.run(self)

setup(name='harmonv',
      version='0.2',
      packages=['harmonv'],
      cmdclass={'install': CustomInstall, 'develop': CustomDevelop},
      package_data={'harmonv': ['ptx/*.cfg', 'ptx/*.interp', 'ptx/*.tokens']},
      scripts=['bin/hcuobjdump',
               'bin/gen_xlat_metadata.py',
               'bin/fbextractor.py'],
      install_requires = ['pyelftools', 'lz4', 'PyYAML']
)
