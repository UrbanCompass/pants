# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import logging.config
import os
import sys

import pkg_resources

from pants.backend.core.tasks.task import QuietTaskMixin
from pants.backend.jvm.tasks.nailgun_task import NailgunTask  # XXX(pl)
from pants.base.build_environment import get_buildroot
from pants.base.build_file import BuildFile
from pants.base.build_file_address_mapper import BuildFileAddressMapper
from pants.base.build_file_parser import BuildFileParser
from pants.base.build_graph import BuildGraph
from pants.base.cmd_line_spec_parser import CmdLineSpecParser
from pants.base.config import Config
from pants.base.extension_loader import load_plugins_and_backends
from pants.base.workunit import WorkUnit
from pants.engine.round_engine import RoundEngine
from pants.goal.context import Context
from pants.goal.goal import Goal
from pants.goal.initialize_reporting import initial_reporting, update_reporting
from pants.goal.run_tracker import RunTracker
from pants.option.global_options import register_global_options
from pants.option.options_bootstrapper import OptionsBootstrapper
from pants.reporting.report import Report
from pants.util.dirutil import safe_mkdir


logger = logging.getLogger(__name__)


class GoalRunner(object):
  """Lists installed goals or else executes a named goal."""

  def __init__(self, root_dir):
    """
    :param root_dir: The root directory of the pants workspace.
    """
    self.root_dir = root_dir

  def setup(self):
    options_bootstrapper = OptionsBootstrapper()

    # Force config into the cache so we (and plugin/backend loading code) can use it.
    # TODO: Plumb options in explicitly.
    bootstrap_options = options_bootstrapper.get_bootstrap_options()
    self.config = Config.from_cache()

    # Add any extra paths to python path (eg for loading extra source backends)
    for path in bootstrap_options.for_global_scope().pythonpath:
      sys.path.append(path)
      pkg_resources.fixup_namespace_packages(path)

    # Load plugins and backends.
    backend_packages = self.config.getlist('backends', 'packages', [])
    plugins = self.config.getlist('backends', 'plugins', [])
    build_configuration = load_plugins_and_backends(plugins, backend_packages)

    # Now that plugins and backends are loaded, we can gather the known scopes.
    self.targets = []
    # TODO: Create a 'Subsystem' abstraction instead of special-casing run-tracker here
    # and in register_options().
    known_scopes = ['', 'run-tracker']
    for goal in Goal.all():
      # Note that enclosing scopes will appear before scopes they enclose.
      known_scopes.extend(filter(None, goal.known_scopes()))

    # Now that we have the known scopes we can get the full options.
    self.options = options_bootstrapper.get_full_options(known_scopes=known_scopes)
    self.register_options()

    # TODO(Eric Ayers) We are missing log messages. Set the log level earlier
    # Enable standard python logging for code with no handle to a context/work-unit.
    self._setup_logging()  # NB: self.options are needed for this call.

    self.run_tracker = RunTracker.from_options(self.options)
    report = initial_reporting(self.config, self.run_tracker)
    self.run_tracker.start(report)
    url = self.run_tracker.run_info.get_info('report_url')
    if url:
      self.run_tracker.log(Report.INFO, 'See a report at: %s' % url)
    else:
      self.run_tracker.log(Report.INFO, '(To run a reporting server: ./pants server)')

    self.build_file_parser = BuildFileParser(build_configuration=build_configuration,
                                             root_dir=self.root_dir,
                                             run_tracker=self.run_tracker)
    self.address_mapper = BuildFileAddressMapper(self.build_file_parser)
    self.build_graph = BuildGraph(run_tracker=self.run_tracker,
                                  address_mapper=self.address_mapper)

    with self.run_tracker.new_workunit(name='bootstrap', labels=[WorkUnit.SETUP]):
      # construct base parameters to be filled in for BuildGraph
      for path in self.config.getlist('goals', 'bootstrap_buildfiles', default=[]):
        build_file = BuildFile.from_cache(root_dir=self.root_dir, relpath=path)
        # TODO(pl): This is an unfortunate interface leak, but I don't think
        # in the long run that we should be relying on "bootstrap" BUILD files
        # that do nothing except modify global state.  That type of behavior
        # (e.g. source roots, goal registration) should instead happen in
        # project plugins, or specialized configuration files.
        self.build_file_parser.parse_build_file_family(build_file)

    # Now that we've parsed the bootstrap BUILD files, and know about the SCM system.
    self.run_tracker.run_info.add_scm_info()

    self._expand_goals_and_specs()

  @property
  def spec_excludes(self):
    # Note: Only call after register_options() has been called.
    return self.options.for_global_scope().spec_excludes

  @property
  def global_options(self):
    return self.options.for_global_scope()

  def register_options(self):
    # Add a 'bootstrap' attribute to the register function, so that register_global can
    # access the bootstrap option values.
    def register_global(*args, **kwargs):
      return self.options.register_global(*args, **kwargs)
    register_global.bootstrap = self.options.bootstrap_option_values()
    register_global_options(register_global)

    # This is the first case we have of non-task, non-global options.
    # The current implementation special-cases RunTracker, and is temporary.
    # In the near future it will be replaced with a 'Subsystem' abstraction.
    # But for now this is useful for kicking the tires.
    def register_run_tracker(*args, **kwargs):
      self.options.register('run-tracker', *args, **kwargs)
    RunTracker.register_options(register_run_tracker)

    for goal in Goal.all():
      goal.register_options(self.options)

  def _expand_goals_and_specs(self):
    goals = self.options.goals
    specs = self.options.target_specs
    fail_fast = self.options.for_global_scope().fail_fast

    for goal in goals:
      if BuildFile.from_cache(get_buildroot(), goal, must_exist=False).exists():
        logger.warning(" Command-line argument '{0}' is ambiguous and was assumed to be "
                       "a goal. If this is incorrect, disambiguate it with ./{0}.".format(goal))

    if self.options.print_help_if_requested():
      sys.exit(0)

    self.requested_goals = goals

    with self.run_tracker.new_workunit(name='setup', labels=[WorkUnit.SETUP]):
      spec_parser = CmdLineSpecParser(self.root_dir, self.address_mapper,
                                      spec_excludes=self.spec_excludes,
                                      exclude_target_regexps=self.global_options.exclude_target_regexp)
      with self.run_tracker.new_workunit(name='parse', labels=[WorkUnit.SETUP]):
        for spec in specs:
          for address in spec_parser.parse_addresses(spec, fail_fast):
            self.build_graph.inject_address_closure(address)
            self.targets.append(self.build_graph.get_target(address))
    self.goals = [Goal.by_name(goal) for goal in goals]

  def run(self):
    def fail():
      self.run_tracker.set_root_outcome(WorkUnit.FAILURE)

    kill_nailguns = self.options.for_global_scope().kill_nailguns
    try:
      result = self._do_run()
      if result:
        fail()
    except KeyboardInterrupt:
      fail()
      # On ctrl-c we always kill nailguns, otherwise they might keep running
      # some heavyweight compilation and gum up the system during a subsequent run.
      kill_nailguns = True
      raise
    except Exception:
      fail()
      raise
    finally:
      self.run_tracker.end()
      # Must kill nailguns only after run_tracker.end() is called, otherwise there may still
      # be pending background work that needs a nailgun.
      if kill_nailguns:
        # TODO: This is JVM-specific and really doesn't belong here.
        # TODO: Make this more selective? Only kill nailguns that affect state?
        # E.g., checkstyle may not need to be killed.
        NailgunTask.killall()
    return result

  def _do_run(self):
    # Update the reporting settings, now that we have flags etc.
    def is_quiet_task():
      for goal in self.goals:
        if goal.has_task_of_type(QuietTaskMixin):
          return True
      return False

    is_explain = self.global_options.explain
    update_reporting(self.global_options, is_quiet_task() or is_explain, self.run_tracker)

    context = Context(
      config=self.config,
      options=self.options,
      run_tracker=self.run_tracker,
      target_roots=self.targets,
      requested_goals=self.requested_goals,
      build_graph=self.build_graph,
      build_file_parser=self.build_file_parser,
      address_mapper=self.address_mapper,
      spec_excludes=self.spec_excludes
    )

    unknown = []
    for goal in self.goals:
      if not goal.ordered_task_names():
        unknown.append(goal)

    if unknown:
      context.log.error('Unknown goal(s): %s\n' % ' '.join(goal.name for goal in unknown))
      return 1

    engine = RoundEngine()
    return engine.execute(context, self.goals)

  def _setup_logging(self):
    # TODO(John Sirois): Consider moving to straight python logging.  The divide between the
    # context/work-unit logging and standard python logging doesn't buy us anything.

    # TODO(John Sirois): Support logging.config.fileConfig so a site can setup fine-grained
    # logging control and we don't need to be the middleman plumbing an option for each python
    # standard logging knob.

    # NB: quiet help says 'Squelches all console output apart from errors'.
    level = 'ERROR' if self.global_options.quiet else self.global_options.level.upper()

    logging_config = {'version': 1,  # required and there is only a version 1 format so far.
                      'disable_existing_loggers': False}

    formatters_config = {'brief': {'format': '%(levelname)s] %(message)s'}}
    handlers_config = {'console': {'class': 'logging.StreamHandler',
                                   'formatter': 'brief',  # defined above
                                   'level': level}}

    log_dir = self.global_options.logdir
    if log_dir:
      safe_mkdir(log_dir)

      # This is close to but not quite glog format.  Namely the leading levelname is not a single
      # character and the fractional second is only to millis precision and not micros.
      glog_date_format = '%m%d %H:%M:%S'
      glog_format = ('%(levelname)s %(asctime)s.%(msecs)d %(process)d %(filename)s:%(lineno)d] '
                     '%(message)s')

      formatters_config['glog'] = {'format': glog_format, 'datefmt': glog_date_format}
      handlers_config['file'] = {'class': 'logging.handlers.RotatingFileHandler',
                                 'formatter': 'glog',  # defined above
                                 'level': level,
                                 'filename': os.path.join(log_dir, 'pants.log'),
                                 'maxBytes': 10 * 1024 * 1024,
                                 'backupCount': 4}

    logging_config['formatters'] = formatters_config
    logging_config['handlers'] = handlers_config
    logging_config['root'] = {'level': level, 'handlers': handlers_config.keys()}

    logging.config.dictConfig(logging_config)
