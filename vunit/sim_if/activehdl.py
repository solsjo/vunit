# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2014-2021, Lars Asplund lars.anders.asplund@gmail.com

"""
Interface towards Aldec Active HDL
"""

from functools import total_ordering
from pathlib import Path
import os
import re
import logging
from ..exceptions import CompileError
from ..ostools import Process, write_file, file_exists, renew_path
from ..vhdl_standard import VHDL
from ..test.suites import get_result_file_name
from . import SimulatorInterface, ListOfStringOption, StringOption
from .vsim_simulator_mixin import get_is_test_suite_done_tcl, fix_path

LOGGER = logging.getLogger(__name__)


class ActiveHDLInterface(SimulatorInterface):
    """
    Active HDL interface
    """

    name = "activehdl"
    supports_gui_flag = True
    package_users_depend_on_bodies = True
    compile_options = [
        ListOfStringOption("activehdl.vcom_flags"),
        ListOfStringOption("activehdl.vlog_flags"),
    ]

    sim_options = [
        ListOfStringOption("activehdl.vsim_flags"),
        ListOfStringOption("activehdl.vsim_flags.gui"),
        StringOption("activehdl.init_file.gui"),
    ]

    @classmethod
    def from_args(cls, args, output_path, elaborate_only=False, precompiled=None, **kwargs):
        """
        Create new instance from command line arguments object
        """
        return cls(
            prefix=cls.find_prefix(),
            output_path=output_path,
            gui=args.gui,
            elaborate_only=elaborate_only,
            precompiled=precompiled,
        )

    @classmethod
    def find_prefix_from_path(cls):
        return cls.find_toolchain(["vsim", "avhdl"])

    @classmethod
    def supports_vhdl_package_generics(cls):
        """
        Returns True when this simulator supports VHDL package generics
        """
        proc = Process([str(Path(cls.find_prefix()) / "vcom"), "-version"], env=cls.get_env())
        consumer = VersionConsumer()
        proc.consume_output(consumer)
        if consumer.version is not None:
            return consumer.version >= Version(10, 1)

        return False

    @staticmethod
    def supports_coverage():
        """
        Returns True when the simulator supports coverage
        """
        return True

    def __init__(self, prefix, output_path, gui=False, elaborate_only=False, precompiled=None):
        SimulatorInterface.__init__(self, output_path, gui, elaborate_only, precompiled)
        self._library_cfg = str(Path(output_path) / "library.cfg")
        self._prefix = prefix
        self._create_library_cfg()
        self._libraries = []
        self._coverage_files = set()

    def setup_library_mapping(self, project):
        """
        Setup library mapping
        """
        mapped_libraries = self._get_mapped_libraries()

        for library in project.get_libraries():
            self._libraries.append(library)
            self.create_library(library.name, library.directory, mapped_libraries)

    def compile_source_file_command(self, source_file):
        """
        Returns the command to compile a single source_file
        """
        if source_file.is_vhdl:
            return self.compile_vhdl_file_command(source_file)

        if source_file.is_any_verilog:
            return self.compile_verilog_file_command(source_file)

        LOGGER.error("Unknown file type: %s", source_file.file_type)
        raise CompileError

    @staticmethod
    def _std_str(vhdl_standard):
        """
        Convert standard to format of Active-HDL command line flag
        """
        if vhdl_standard <= VHDL.STD_2008:
            return "-%s" % vhdl_standard

        raise ValueError("Invalid VHDL standard %s" % vhdl_standard)

    def compile_vhdl_file_command(self, source_file):
        """
        Returns the command to compile a VHDL file
        """
        return (
            [
                str(Path(self._prefix) / "vcom"),
                "-quiet",
                "-j",
                str(Path(self._library_cfg).parent),
            ]
            + source_file.compile_options.get("activehdl.vcom_flags", [])
            + [
                self._std_str(source_file.get_vhdl_standard()),
                "-work",
                source_file.library.name,
                source_file.name,
            ]
        )

    def compile_verilog_file_command(self, source_file):
        """
        Returns the command to compile a Verilog file
        """
        args = [str(Path(self._prefix) / "vlog"), "-quiet", "-lc", self._library_cfg]
        args += source_file.compile_options.get("activehdl.vlog_flags", [])
        args += ["-work", source_file.library.name, source_file.name]
        for library in self._libraries:
            args += ["-l", library.name]
        for include_dir in source_file.include_dirs:
            args += ["+incdir+%s" % include_dir]
        for key, value in source_file.defines.items():
            args += ["+define+%s=%s" % (key, value)]
        return args

    def create_library(self, library_name, path, mapped_libraries=None):
        """
        Create and map a library_name to path
        """
        mapped_libraries = mapped_libraries if mapped_libraries is not None else {}

        apath = str(Path(path).parent.resolve())

        if not file_exists(apath):
            os.makedirs(apath)

        if not file_exists(path):
            proc = Process(
                [str(Path(self._prefix) / "vlib"), library_name, path],
                cwd=str(Path(self._library_cfg).parent),
                env=self.get_env(),
            )
            proc.consume_output(callback=None)

        if library_name in mapped_libraries and mapped_libraries[library_name] == path:
            return

        proc = Process(
            [str(Path(self._prefix) / "vmap"), library_name, path],
            cwd=str(Path(self._library_cfg).parent),
            env=self.get_env(),
        )
        proc.consume_output(callback=None)

    def _create_library_cfg(self):
        """
        Create the library.cfg file if it does not exist
        """
        if file_exists(self._library_cfg):
            return

        with Path(self._library_cfg).open("w", encoding="utf-8") as ofile:
            ofile.write('$INCLUDE = "%s"\n' % str(Path(self._prefix).parent / "vlib" / "library.cfg"))

    _library_re = re.compile(r'([a-zA-Z_]+)\s=\s"(.*)"')

    def _get_mapped_libraries(self):
        """
        Get mapped libraries from library.cfg file
        """
        with Path(self._library_cfg).open("r", encoding="utf-8") as fptr:
            text = fptr.read()

        libraries = {}
        for line in text.splitlines():
            match = self._library_re.match(line)
            if match is None:
                continue
            key = match.group(1)
            value = match.group(2)
            libraries[key] = str((Path(self._library_cfg).parent / Path(value).parent).resolve())
        return libraries

    def _vsim_extra_args(self, config):
        """
        Determine vsim_extra_args
        """
        vsim_extra_args = []
        vsim_extra_args = config.sim_options.get("activehdl.vsim_flags", vsim_extra_args)

        if self._gui:
            vsim_extra_args = config.sim_options.get("activehdl.vsim_flags.gui", vsim_extra_args)

        return " ".join(vsim_extra_args)

    def _create_load_function(self, config, output_path):
        """
        Create the vunit_load TCL function that runs the vsim command and loads the design
        """
        set_generic_str = "\n    ".join(
            ("set vunit_generic_%s {%s}" % (name, value) for name, value in config.generics.items())
        )
        set_generic_name_str = " ".join(
            ("-g/%s/%s=${vunit_generic_%s}" % (config.entity_name, name, name) for name in config.generics)
        )
        pli_str = " ".join('-pli "%s"' % fix_path(name) for name in config.sim_options.get("pli", []))

        vsim_flags = [
            pli_str,
            set_generic_name_str,
            "-lib",
            config.library_name,
            config.entity_name,
        ]

        if config.architecture_name is not None:
            vsim_flags.append(config.architecture_name)

        if config.sim_options.get("enable_coverage", False):
            coverage_file_path = str(Path(output_path) / "coverage.acdb")
            self._coverage_files.add(coverage_file_path)
            vsim_flags += ["-acdb_file {%s}" % fix_path(coverage_file_path)]

        vsim_flags += [self._vsim_extra_args(config)]

        if config.sim_options.get("disable_ieee_warnings", False):
            vsim_flags.append("-ieee_nowarn")

        # Add the the testbench top-level unit last as coverage is
        # only collected for the top-level unit specified last

        vhdl_assert_stop_level_mapping = dict(warning=1, error=2, failure=3)

        tcl = """
proc vunit_load {{}} {{
    {set_generic_str}
    set vsim_failed [catch {{
        vsim {vsim_flags}
    }}]
    if {{${{vsim_failed}}}} {{
        return true
    }}

    global breakassertlevel
    set breakassertlevel {breaklevel}

    global builtinbreakassertlevel
    set builtinbreakassertlevel $breakassertlevel

    return false
}}
""".format(
            set_generic_str=set_generic_str,
            vsim_flags=" ".join(vsim_flags),
            breaklevel=vhdl_assert_stop_level_mapping[config.vhdl_assert_stop_level],
        )

        return tcl

    @staticmethod
    def _create_run_function():
        """
        Create the vunit_run function to run the test bench
        """
        return """
proc vunit_run {} {
    run -all
    if {![is_test_suite_done]} {
        catch {
            # tb command can fail when error comes from pli
            echo ""
            echo "Stack trace result from 'bt' command"
            bt
        }
        return true;
    }
    return false;
}
"""

    def merge_coverage(self, file_name, args=None):
        """
        Merge coverage from all test cases,
        """

        merge_command = "onerror {quit -code 1}\n"
        merge_command += "acdb merge"

        for coverage_file in self._coverage_files:
            if file_exists(coverage_file):
                merge_command += " -i {%s}" % fix_path(coverage_file)
            else:
                LOGGER.warning("Missing coverage file: %s", coverage_file)

        if args is not None:
            merge_command += " " + " ".join("{%s}" % arg for arg in args)

        merge_command += " -o {%s}" % fix_path(file_name) + "\n"

        merge_script_name = str(Path(self._output_path) / "acdb_merge.tcl")
        with Path(merge_script_name).open("w", encoding="utf-8") as fptr:
            fptr.write(merge_command + "\n")

        vcover_cmd = [
            str(Path(self._prefix) / "vsimsa"),
            "-tcl",
            "%s" % fix_path(merge_script_name),
        ]

        print("Merging coverage files into %s..." % file_name)
        vcover_merge_process = Process(vcover_cmd, env=self.get_env())
        vcover_merge_process.consume_output()
        print("Done merging coverage files")

    def _create_common_script(self, config, output_path):
        """
        Create tcl script with functions common to interactive and batch modes
        """
        tcl = ""
        tcl += get_is_test_suite_done_tcl(get_result_file_name(output_path))
        tcl += self._create_load_function(config, output_path)
        tcl += self._create_run_function()
        return tcl

    @staticmethod
    def _create_batch_script(common_file_name, load_only=False):
        """
        Create tcl script to run in batch mode
        """
        batch_do = ""
        batch_do += 'source "%s"\n' % fix_path(common_file_name)
        batch_do += "set failed [vunit_load]\n"
        batch_do += "if {$failed} {quit -code 1}\n"
        if not load_only:
            batch_do += "set failed [vunit_run]\n"
            batch_do += "if {$failed} {quit -code 1}\n"
        batch_do += "quit -code 0\n"
        return batch_do

    def _create_gui_script(self, common_file_name, config):
        """
        Create the user facing script which loads common functions and prints a help message
        """

        tcl = ""
        tcl += 'source "%s"\n' % fix_path(common_file_name)
        tcl += "workspace create workspace\n"
        tcl += "design create -a design .\n"

        for library in self._libraries:
            tcl += "vmap %s %s\n" % (library.name, fix_path(library.directory))

        tcl += "vunit_load\n"

        init_file = config.sim_options.get(self.name + ".init_file.gui", None)
        if init_file is not None:
            tcl += 'source "%s"\n' % fix_path(str(Path(init_file).resolve()))

        tcl += 'puts "VUnit help: Design already loaded. Use run -all to run the test."\n'

        return tcl

    def _run_batch_file(self, batch_file_name, gui, cwd):
        """
        Run a test bench in batch by invoking a new vsim process from the command line
        """

        todo = '@do -tcl ""%s""' % fix_path(batch_file_name)
        if not gui:
            todo = "@onerror {quit -code 1};" + todo

        try:
            args = [
                str(Path(self._prefix) / "vsim"),
                "-gui" if gui else "-c",
                "-l",
                str(Path(batch_file_name).parent / "transcript"),
                "-do",
                todo,
            ]

            proc = Process(args, cwd=cwd, env=self.get_env())
            proc.consume_output()
        except Process.NonZeroExitCode:
            return False
        return True

    def simulate(self, output_path, test_suite_name, config):
        """
        Run a test bench
        """
        script_path = Path(output_path) / self.name
        common_file_name = script_path / "common.tcl"
        batch_file_name = script_path / "batch.tcl"
        gui_file_name = script_path / "gui.tcl"

        write_file(common_file_name, self._create_common_script(config, output_path))
        write_file(gui_file_name, self._create_gui_script(str(common_file_name), config))
        write_file(
            str(batch_file_name),
            self._create_batch_script(str(common_file_name), self.elaborate_only),
        )

        if self._gui:
            gui_path = str(script_path / "gui")
            renew_path(gui_path)
            return self._run_batch_file(str(gui_file_name), gui=True, cwd=gui_path)

        return self._run_batch_file(str(batch_file_name), gui=False, cwd=str(Path(self._library_cfg).parent))


@total_ordering
class Version(object):
    """
    Simulator version
    """

    def __init__(self, major=0, minor=0, minor_letter=""):
        self.major = major
        self.minor = minor
        self.minor_letter = minor_letter

    def _compare(self, other, greater_than, less_than, equal_to):
        """
        Compares this object with another
        """
        if self.major > other.major:
            result = greater_than
        elif self.major < other.major:
            result = less_than
        elif self.minor > other.minor:
            result = greater_than
        elif self.minor < other.minor:
            result = less_than
        elif self.minor_letter > other.minor_letter:
            result = greater_than
        elif self.minor_letter < other.minor_letter:
            result = less_than
        else:
            result = equal_to

        return result

    def __lt__(self, other):
        return self._compare(other, greater_than=False, less_than=True, equal_to=False)

    def __eq__(self, other):
        return self._compare(other, greater_than=False, less_than=False, equal_to=True)


class VersionConsumer(object):
    """
    Consume version information
    """

    def __init__(self):
        self.version = None

    _version_re = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)(?P<minor_letter>[a-zA-Z]?)\.\d+\.\d+")

    def __call__(self, line):
        match = self._version_re.search(line)
        if match is not None:
            major = int(match.group("major"))
            minor = int(match.group("minor"))
            minor_letter = match.group("minor_letter")
            self.version = Version(major, minor, minor_letter)
        return True
