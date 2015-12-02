# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
import subprocess

from pants.base.build_environment import get_buildroot
from pants.base.exceptions import TaskError
from pants.base.workunit import WorkUnitLabel
from pants.task.task import Task
from pants.util.dirutil import safe_mkdir

from pants.contrib.haskell.subsystems.stack_distribution import StackDistribution
from pants.contrib.haskell.targets.haskell_hackage_package import HaskellHackagePackage
from pants.contrib.haskell.targets.haskell_project import HaskellProject
from pants.contrib.haskell.targets.haskell_source_package import HaskellSourcePackage
from pants.contrib.haskell.targets.haskell_stackage_package import HaskellStackagePackage


class StackTask(Task):
  """Abstract class that all other `stack` tasks inherit from"""

  @classmethod
  def global_subsystems(cls):
    return super(StackTask, cls).global_subsystems() + (StackDistribution.Factory,)

  @property
  def cache_target_dirs(self):
    return True

  @staticmethod
  def is_haskell_stackage_package(target):
    """Test if the given `Target` is a `HaskellStackagePackage` target

    :param target: The target to test
    :type target: :class:`pants.build_graph.target.Target`
    :returns: `True` if the target is a :class:`pants.contrib.haskell.targets.haskell_stackage_package.HaskellStackagePackage` target, `False` otherwise
    :rtype: bool
    """
    return isinstance(target, HaskellStackagePackage)

  @staticmethod
  def is_haskell_hackage_package(target):
    """Test if the given `Target` is a `HaskellHackagePackage` target

    :param target: The target to test
    :type target: :class:`pants.build_graph.target.Target`
    :returns: `True` if the target is a :class:`pants.contrib.haskell.targets.haskell_hackage_package.HaskellHackagePackage` target, `False` otherwise
    :rtype: bool
    """
    return isinstance(target, HaskellHackagePackage)

  @staticmethod
  def is_haskell_source_package(target):
    """Test if the given `Target` is a `HaskellSourcePackage` target

    :param target: The target to test
    :type target: :class:`pants.build_graph.target.Target`
    :returns: `True` if the target is a :class:`pants.contrib.haskell.targets.haskell_source_package.HaskellSourcePackage` target, `False` otherwise
    :rtype: bool
    """
    return isinstance(target, HaskellSourcePackage)

  @staticmethod
  def is_haskell_project(target):
    """Test if the given `Target` is a `HaskellProject` target

    :param target: The target to test
    :type target: :class:`pants.build_graph.target.Target`
    :returns: `True` if the target is a :class:`pants.contrib.haskell.targets.haskell_project.HaskellProject` target, `False` otherwise
    :rtype: bool
    """
    return isinstance(target, HaskellProject)

  @staticmethod
  def make_stack_yaml(target):
    """Build a `stack.yaml` file from a root target's dependency graph:

    * Every `stackage` target is currently ignored since they are already covered
      by the `resolver` field
    * Every `hackage` target translates to an `extra-deps` entry
    * Every `cabal` target translates to a `package` entry

    :param target: The pants target to build a `stack.yaml` for.
    :type target: :class:`pants.build_graph.target.Target`
    :returns: The string contents to use for the generated `stack.yaml` file.
    :rtype: str
    :raises: :class:`pants.base.exceptions.TaskError` when the target's
             dependency graph specifies multiple different resolvers.
    """
    packages = list(target.closure())
    hackage_packages = filter(StackTask.is_haskell_hackage_package, packages)
    source_packages  = filter(StackTask.is_haskell_source_package , packages)

    yaml = 'flags: {}\n'

    if source_packages:
      yaml += 'packages:\n'
      for pkg in source_packages:
        path = pkg.path or os.path.join(get_buildroot(), pkg.target_base)
        yaml += '- ' + path + '\n'
    else:
      yaml += 'packages: []\n'

    if hackage_packages:
      yaml += 'extra-deps:\n'
      for pkg in hackage_packages:
        yaml += '- ' + pkg.package + '-' + pkg.version + '\n'
    else:
      yaml += 'extra-deps: []\n'

    yaml += 'resolver: ' + target.resolver + '\n'

    return yaml

  def stack_task(self, command, vt, cmd_args = []):
    """
    This function provides shared logic for all `StackTask` sub-classes, which
    consists of:

    * creating a `stack.yaml` file within that target's cached results directory
    * invoking `stack` from within that directory

    Any executables generated by the `stack` command will be stored in a `bin/`
    subdirectory of the cached results directory.

    :param str command: The `stack` sub-command to run (i.e. "build" or "ghci").
    :param vt: The root target that `stack` should operate on.
    :type vt: :class:`pants.invalidation.cache_manager.VersionedTarget`
    :param cmd_args: Additional flags to pass through to the `stack` subcommand.
    :type cmd_args: list of strings
    :raises: :class:`pants.base.exceptions.TaskError` when the `stack`
             subprocess returns a non-zero exit code
    """
    yaml = StackTask.make_stack_yaml(vt.target)

    stack_yaml_path = os.path.join(vt.results_dir, 'stack.yaml')
    with open(stack_yaml_path, 'w') as handle:
      handle.write(yaml)

    bin_path = os.path.join(vt.results_dir, 'bin')
    safe_mkdir(bin_path)

    packages = list(vt.target.closure())
    hackage_packages  = filter(StackTask.is_haskell_hackage_package , packages)
    stackage_packages = filter(StackTask.is_haskell_stackage_package, packages)
    source_packages   = filter(StackTask.is_haskell_source_package  , packages)
    haskell_packages = hackage_packages + stackage_packages + source_packages
    haskell_package_names = map(lambda p: p.package, haskell_packages)

    stack_args = [
      '--local-bin-path', bin_path,
      '--stack-yaml', stack_yaml_path,
    ]

    cmd_args = haskell_package_names + cmd_args

    try:
      stack_distribution = StackDistribution.Factory.create()
      stack_distribution.execute_stack_cmd(
        command,
        stack_args=stack_args,
        cmd_args=cmd_args,
        workunit_factory=self.context.new_workunit,
        workunit_name='stack-run',
        workunit_labels=[WorkUnitLabel.TOOL],
        )
    except subprocess.CalledProcessError:
      raise TaskError("""
`stack` subprocess failed with the following inputs:

Arguments: {args}
Contents of {stack_yaml_path}:

```
{yaml}
```
""".strip().format(stack_yaml_path=stack_yaml_path,
                   yaml=yaml,
                   args=args))
      raise

  def execute(self):
    pass