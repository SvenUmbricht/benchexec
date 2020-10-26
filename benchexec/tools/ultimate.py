# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2015-2020 Daniel Dietsch <dietsch@informatik.uni-freiburg.de>
# SPDX-FileCopyrightText: 2015-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import functools
import glob
import logging
import os
import re
import shutil
import subprocess
import sys

import benchexec.result as result
import benchexec.tools.template
import benchexec.util as util
from benchexec import BenchExecException
from benchexec.model import MEMLIMIT
from benchexec.tools.template import UnsupportedFeatureException

_OPTION_NO_WRAPPER = "--force-no-wrapper"
_SVCOMP17_VERSIONS = {"f7c3ed31"}
_SVCOMP17_FORBIDDEN_FLAGS = {"--full-output", "--architecture"}
_ULTIMATE_VERSION_REGEX = re.compile(r"^Version is (.*)$", re.MULTILINE)
# .jar files that are used as launcher arguments with most recent .jar first
_LAUNCHER_JARS = [
    "plugins/org.eclipse.equinox.launcher_1.5.800.v20200727-1323.jar",
    "plugins/org.eclipse.equinox.launcher_1.3.100.v20150511-1540.jar",
]


class UltimateTool(benchexec.tools.template.BaseTool2):
    """
    Abstract tool info for Ultimate-based tools.
    """

    REQUIRED_PATHS = [
        "artifacts.xml",
        "config",
        "configuration",
        "cvc4",
        "cvc4nyu",
        "cvc4-LICENSE",
        "features",
        "LICENSE",
        "LICENSE.GPL",
        "LICENSE.GPL.LESSER",
        "mathsat",
        "mathsat-LICENSE",
        "p2",
        "plugins",
        "README",
        "Ultimate",
        "Ultimate.ini",
        "Ultimate.py",
        "z3",
        "z3-LICENSE",
    ]

    REQUIRED_PATHS_SVCOMP17 = []

    def __init__(self):
        self._uses_propertyfile = False
        self.java = None

    @functools.lru_cache()
    def executable(self, tool_locator):
        exe = None
        try:
            exe = tool_locator.find_executable("Ultimate.py")
            for (_, dir_names, file_names) in os.walk(os.path.dirname(exe)):
                if "Ultimate" in file_names and "plugins" in dir_names:
                    return exe
                break
        except benchexec.tools.template.ToolNotFoundException:
            pass

        # possibly another Ultimate.py was found or not found at all, check in the current dir
        current = os.getcwd()
        for (_, dir_names, file_names) in os.walk(current):
            if (
                "Ultimate" in file_names
                and "Ultimate.py" in file_names
                and "plugins" in dir_names
            ):
                return "./Ultimate.py"
            break

        msg = "ERROR: Could not find Ultimate executable in '{0}' or '{1}'".format(
            str(exe), str(current)
        )
        raise benchexec.tools.template.ToolNotFoundException(msg)

    def _ultimate_version(self, executable):
        data_dir = os.path.join(os.path.dirname(executable), "data")
        launcher_jar = self._get_current_launcher_jar(executable)
        java_versions = self.get_java_installations()
        cmds = [
            # 2
            [
                "-Xss4m",
                "-jar",
                launcher_jar,
                "-data",
                "@noDefault",
                "-ultimatedata",
                data_dir,
                "--version",
            ],
            # 1
            ["-Xss4m", "-jar", launcher_jar, "-data", data_dir, "--version"],
        ]

        self.api = len(cmds)
        for cmd in cmds:
            for java_version, java in java_versions.items():
                version = self._query_ultimate_version([java] + cmd, self.api)
                if version:
                    logging.debug(
                        "Using Java %s with version %s for API version %s of Ultimate %s",
                        java,
                        java_version,
                        self.api,
                        version,
                    )
                    self.java = java
                    return version
            self.api = self.api - 1
        raise BenchExecException("Cannot determine Ultimate version")

    def _query_ultimate_version(self, cmd, api):
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            (stdout, stderr) = process.communicate()
        except OSError as e:
            logging.warning(
                "Cannot run Java to determine Ultimate version (API %s): %s",
                api,
                e.strerror,
            )
            return ""
        stdout = util.decode_to_string(stdout).strip()
        if stderr or process.returncode:
            logging.warning("Cannot determine Ultimate version (API %s)", api)
            logging.debug(
                "Command was:     %s\n"
                "Exit code:       %s\n"
                "Error output:    %s\n"
                "Standard output: %s",
                " ".join(map(util.escape_string_shell, cmd)),
                process.returncode,
                util.decode_to_string(stderr),
                stdout,
            )
            return ""

        version_ultimate_match = _ULTIMATE_VERSION_REGEX.search(stdout)
        if not version_ultimate_match:
            logging.warning(
                "Cannot determine Ultimate version (API %s), output was: %s",
                api,
                stdout,
            )
            return ""
        return version_ultimate_match.group(1)

    @functools.lru_cache()
    def _get_current_launcher_jar(self, executable):
        ultimate_dir = os.path.dirname(executable)
        for jar in _LAUNCHER_JARS:
            launcher_jar = os.path.join(ultimate_dir, jar)
            if os.path.isfile(launcher_jar):
                return launcher_jar
        raise FileNotFoundError(
            "No suitable launcher jar found in {0}".format(ultimate_dir)
        )

    @functools.lru_cache()
    def version(self, executable):
        wrapper_version = self._version_from_tool(executable)
        if wrapper_version in _SVCOMP17_VERSIONS:
            # Keep reported version number for old versions as they were before
            return wrapper_version

        ultimate_version = self._ultimate_version(executable)
        return ultimate_version + "-" + wrapper_version

    @functools.lru_cache()
    def _is_svcomp17_version(self, executable):
        return self.version(executable) in _SVCOMP17_VERSIONS

    @functools.lru_cache()
    def _requires_ultimate_data(self, executable):
        if self._is_svcomp17_version(executable):
            return False

        version = self.version(executable)
        ult, wrapper = version.split("-")
        major, minor, patch = ult.split(".")
        # all versions before 0.1.24 do not require ultimatedata
        return not (int(major) == 0 and int(minor) < 2 and int(patch) < 24)

    def cmdline(self, executable, options, task, resource_limits):
        self._uses_propertyfile = task.property_file is not None
        combined_options = options + [*task.options] if task.options else []

        if _OPTION_NO_WRAPPER in combined_options:
            # do not use old wrapper script even if property file is given
            self._uses_propertyfile = False
            combined_options.remove(_OPTION_NO_WRAPPER)

        if self._is_svcomp17_version(executable):
            return self._cmdline_svcomp17(executable, combined_options, task)

        if self._uses_propertyfile:
            return self._cmdline_default(executable, combined_options, task)

        # if no property file is given and toolchain (-tc) is, use Ultimate directly and
        # ignore executable (old executable is just around for backwards compatibility)
        if "-tc" in combined_options or "--toolchain" in combined_options:
            return self._cmdline_no_wrapper(
                executable, combined_options, task, resource_limits
            )

        # there is no way to run ultimate; not enough parameters
        msg = (
            "Unsupported argument combination: You either need a property file or a toolchain option (-tc). "
            "options={} property_file={} resource_limits={}".format(
                options, task.property_file, resource_limits
            )
        )
        raise UnsupportedFeatureException(msg)

    def _cmdline_no_wrapper(self, executable, options, task, resource_limits):
        mem_bytes = resource_limits.memory
        cmdline = [self.java]

        # -ea has to be given directly to java
        if "-ea" in options:
            options = [e for e in options if e != "-ea"]
            cmdline += ["-ea"]

        if mem_bytes:
            cmdline += ["-Xmx" + str(mem_bytes)]
        cmdline += ["-Xss4m"]
        cmdline += ["-jar", self._get_current_launcher_jar(executable)]

        if self._requires_ultimate_data(executable):
            if "-ultimatedata" not in options and "-data" not in options:
                if self.api == 2:
                    cmdline += [
                        "-data",
                        "@noDefault",
                        "-ultimatedata",
                        os.path.join(os.path.dirname(executable), "data"),
                    ]
                if self.api == 1:
                    raise ValueError(
                        "Illegal option -ultimatedata for API {} and Ultimate version {}".format(
                            self.api, self.version(executable)
                        )
                    )
            elif "-ultimatedata" in options and "-data" not in options:
                if self.api == 2:
                    cmdline += ["-data", "@noDefault"]
                if self.api == 1:
                    raise ValueError(
                        "Illegal option -ultimatedata for API {} and Ultimate version {}".format(
                            self.api, self.version(executable)
                        )
                    )
        else:
            if "-data" not in options:
                if self.api == 2 or self.api == 1:
                    cmdline += [
                        "-data",
                        os.path.join(os.path.dirname(executable), "data"),
                    ]

        cmdline += options

        if task.input_files:
            cmdline += ["-i"] + [*task.input_files]
        self.__assert_cmdline(cmdline, "No_Wrapper")
        return cmdline

    def _cmdline_default(self, executable, options, task):
        # use the old wrapper script if a property file is given
        cmdline = [executable, "--spec", task.property_file]
        if task.input_files:
            cmdline += ["--file"] + [*task.input_files]
        cmdline += options
        self.__assert_cmdline(cmdline, "Default")
        return cmdline

    def _cmdline_svcomp17(self, executable, options, task):
        cmdline = [executable, task.property_file]
        cmdline += [
            option for option in options if option not in _SVCOMP17_FORBIDDEN_FLAGS
        ]
        cmdline.append("--full-output")
        cmdline += [*task.input_files]
        self.__assert_cmdline(cmdline, "SVCOMP17")
        return cmdline

    @staticmethod
    def __assert_cmdline(cmdline, mode):
        assert all(
            cmdline
        ), "cmdline contains empty or None argument when using %s mode: %s".format(
            mode, str(cmdline)
        )
        pass

    def program_files(self, executable):
        paths = (
            self.REQUIRED_PATHS_SVCOMP17
            if self._is_svcomp17_version(executable)
            else self.REQUIRED_PATHS
        )
        return [executable] + self._program_files_from_executable(executable, paths)

    def determine_result(self, run):
        if self._uses_propertyfile:
            return self._determine_result_with_property_file(run)
        return self._determine_result_without_property_file(run)

    def _determine_result_without_property_file(self, run):
        # special strings in ultimate output
        treeautomizer_sat = "TreeAutomizerSatResult"
        treeautomizer_unsat = "TreeAutomizerUnsatResult"
        unsupported_syntax_errorstring = "ShortDescription: Unsupported Syntax"
        incorrect_syntax_errorstring = "ShortDescription: Incorrect Syntax"
        type_errorstring = "Type Error"
        witness_errorstring = "InvalidWitnessErrorResult"
        exception_errorstring = "ExceptionOrErrorResult"
        safety_string = "Ultimate proved your program to be correct"
        all_spec_string = "AllSpecificationsHoldResult"
        unsafety_string = "Ultimate proved your program to be incorrect"
        mem_deref_false_string = "pointer dereference may fail"
        mem_deref_false_string_2 = "array index can be out of bounds"
        mem_free_false_string = "free of unallocated memory possible"
        mem_memtrack_false_string = "not all allocated memory was freed"
        termination_false_string = (
            "Found a nonterminating execution for the following "
            "lasso shaped sequence of statements"
        )
        termination_true_string = "TerminationAnalysisResult: Termination proven"
        ltl_false_string = "execution that violates the LTL property"
        ltl_true_string = "Buchi Automizer proved that the LTL property"
        overflow_false_string = "overflow possible"

        for line in run.output:
            if line.find(unsupported_syntax_errorstring) != -1:
                return "ERROR: UNSUPPORTED SYNTAX"
            if line.find(incorrect_syntax_errorstring) != -1:
                return "ERROR: INCORRECT SYNTAX"
            if line.find(type_errorstring) != -1:
                return "ERROR: TYPE ERROR"
            if line.find(witness_errorstring) != -1:
                return "ERROR: INVALID WITNESS FILE"
            if line.find(exception_errorstring) != -1:
                return "ERROR: EXCEPTION"
            if self._contains_overapproximation_result(line):
                return "UNKNOWN: OverapproxCex"
            if line.find(termination_false_string) != -1:
                return result.RESULT_FALSE_TERMINATION
            if line.find(termination_true_string) != -1:
                return result.RESULT_TRUE_PROP
            if line.find(ltl_false_string) != -1:
                return "FALSE(valid-ltl)"
            if line.find(ltl_true_string) != -1:
                return result.RESULT_TRUE_PROP
            if line.find(unsafety_string) != -1:
                return result.RESULT_FALSE_REACH
            if line.find(mem_deref_false_string) != -1:
                return result.RESULT_FALSE_DEREF
            if line.find(mem_deref_false_string_2) != -1:
                return result.RESULT_FALSE_DEREF
            if line.find(mem_free_false_string) != -1:
                return result.RESULT_FALSE_FREE
            if line.find(mem_memtrack_false_string) != -1:
                return result.RESULT_FALSE_MEMTRACK
            if line.find(overflow_false_string) != -1:
                return result.RESULT_FALSE_OVERFLOW
            if line.find(safety_string) != -1 or line.find(all_spec_string) != -1:
                return result.RESULT_TRUE_PROP
            if line.find(treeautomizer_unsat) != -1:
                return "unsat"
            if line.find(treeautomizer_sat) != -1 or line.find(all_spec_string) != -1:
                return "sat"

        return result.RESULT_UNKNOWN

    @staticmethod
    def _contains_overapproximation_result(line):
        triggers = [
            "Reason: overapproximation of",
            "Reason: overapproximation of bitwiseAnd",
            "Reason: overapproximation of bitwiseOr",
            "Reason: overapproximation of bitwiseXor",
            "Reason: overapproximation of shiftLeft",
            "Reason: overapproximation of shiftRight",
            "Reason: overapproximation of bitwiseComplement",
        ]

        for trigger in triggers:
            if line.find(trigger) != -1:
                return True

        return False

    @staticmethod
    def _determine_result_with_property_file(run):
        for line in run.output:
            if line.startswith("FALSE(valid-free)"):
                return result.RESULT_FALSE_FREE
            elif line.startswith("FALSE(valid-deref)"):
                return result.RESULT_FALSE_DEREF
            elif line.startswith("FALSE(valid-memtrack)"):
                return result.RESULT_FALSE_MEMTRACK
            elif line.startswith("FALSE(valid-memcleanup)"):
                return result.RESULT_FALSE_MEMCLEANUP
            elif line.startswith("FALSE(TERM)"):
                return result.RESULT_FALSE_TERMINATION
            elif line.startswith("FALSE(OVERFLOW)"):
                return result.RESULT_FALSE_OVERFLOW
            elif line.startswith("FALSE"):
                return result.RESULT_FALSE_REACH
            elif line.startswith("TRUE"):
                return result.RESULT_TRUE_PROP
            elif line.startswith("UNKNOWN"):
                return result.RESULT_UNKNOWN
            elif line.startswith("ERROR"):
                status = result.RESULT_ERROR
                if line.startswith("ERROR: INVALID WITNESS FILE"):
                    status += " (invalid witness file)"
                return status
        return result.RESULT_UNKNOWN

    def get_value_from_output(self, output, identifier):
        # search for the text in output and get its value,
        # stop after the first line, that contains the searched text
        for line in output:
            if identifier in line:
                start_position = line.find("=") + 1
                return line[start_position:].strip()
        return None

    def get_java_installations(self):
        candidates = [
            "java",
            "/usr/bin/java",
            "/opt/oracle-jdk-bin-*/bin/java",
            "/opt/openjdk-*/bin/java",
            "/usr/lib/jvm/java-*-openjdk-amd64/bin/java",
        ]

        candidates = [c for entry in candidates for c in glob.glob(entry)]
        pattern = r'"(\d+\.\d+).*"'

        rtr = {}
        for c in candidates:
            candidate = shutil.which(c)
            if not candidate:
                continue
            try:
                process = subprocess.Popen(
                    [candidate, "-version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                (stdout, stderr) = process.communicate()
            except OSError:
                continue

            stdout = util.decode_to_string(stdout).strip()
            if not stdout:
                continue
            version = re.search(pattern, stdout).groups()[0]
            if version not in rtr:
                logging.debug(
                    "Found Java installation %s with version %s", candidate, version
                )
                rtr[version] = candidate
        if not rtr:
            raise BenchExecException("Could not find any Java version")
        return rtr
