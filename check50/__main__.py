import argparse
import contextlib
import enum
import gettext
import importlib
import inspect
import itertools
import json
import logging
import os
import pprint
import site
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import time

import attr
import lib50
import requests
import termcolor

from . import internal, renderer, __version__
from .runner import CheckRunner

LOGGER = logging.getLogger("check50")

lib50.set_local_path(os.environ.get("CHECK50_PATH", "~/.local/share/check50"))


class LogLevel(enum.IntEnum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "ERROR": "red",
        "WARNING": "yellow",
        "INFO": "cyan",
        "DEBUG": "magenta",
    }

    def __init__(self, fmt, use_color=True):
        logging.Formatter.__init__(self, fmt=fmt)
        self.use_color = use_color
        self.pretty_printer = pprint.PrettyPrinter(indent=1, compact=True)

    def format(self, record):
        if not isinstance(record.msg, str):
            record.msg = self.pretty_printer.pformat(record.msg)
        msg = super().format(record)
        return msg if not self.use_color else termcolor.colored(msg, getattr(record, "color", self.COLORS.get(record.levelname)))


class RemoteCheckError(internal.Error):
    def __init__(self, remote_json):
        super().__init__("check50 ran into an error while running checks! Please contact sysadmins@cs50.harvard.edu!")
        self.payload = {"remote_json": remote_json}


@contextlib.contextmanager
def nullcontext(entry_result=None):
    """This is just contextlib.nullcontext but that function is only available in 3.7+."""
    yield entry_result


def excepthook(cls, exc, tb):
    # All channels to output to
    outputs = excepthook.outputs

    for output in excepthook.outputs:
        outputs.remove(output)
        if output == "json":
            ctxmanager = open(excepthook.output_file, "w") if excepthook.output_file else nullcontext(sys.stdout)
            with ctxmanager as output_file:
                json.dump({
                    "slug": excepthook.slug,
                    "error": {
                        "type": cls.__name__,
                        "value": str(exc),
                        "traceback": traceback.format_tb(exc.__traceback__),
                        "data" : exc.payload if hasattr(exc, "payload") else {}
                    },
                    "version": __version__
                }, output_file, indent=4)
                output_file.write("\n")

        elif output == "ansi" or output == "html":
            if (issubclass(cls, internal.Error) or issubclass(cls, lib50.Error)) and exc.args:
                termcolor.cprint(str(exc), "red", file=sys.stderr)
            elif issubclass(cls, FileNotFoundError):
                termcolor.cprint(_("{} not found").format(exc.filename), "red", file=sys.stderr)
            elif issubclass(cls, KeyboardInterrupt):
                termcolor.cprint(f"check cancelled", "red")
            elif not issubclass(cls, Exception):
                # Class is some other BaseException, better just let it go
                return
            else:
                termcolor.cprint(_("Sorry, something's wrong! Let sysadmins@cs50.harvard.edu know!"), "red", file=sys.stderr)

            if excepthook.verbose:
                traceback.print_exception(cls, exc, tb)
                if hasattr(exc, "payload"):
                    print("Exception payload:", json.dumps(exc.payload), sep="\n")

    sys.exit(1)


def yes_no_prompt(prompt):
    """
    Raise a prompt, returns True if yes is entered, False if no is entered.
    Will reraise prompt in case of any other reply.
    """
    yes = {"yes", "ye", "y", ""}
    no = {"no", "n"}

    reply = None
    while reply not in yes and reply not in no:
        reply = input(f"{prompt} [Y/n] ").lower()

    return reply in yes


# Assume we should print tracebacks until we get command line arguments
excepthook.verbose = True
excepthook.output = "ansi"
excepthook.output_file = None
sys.excepthook = excepthook


def install_dependencies(dependencies, verbose=False):
    """Install all packages in dependency list via pip."""
    if not dependencies:
        return

    stdout = stderr = None if verbose else subprocess.DEVNULL
    with tempfile.TemporaryDirectory() as req_dir:
        req_file = Path(req_dir) / "requirements.txt"

        with open(req_file, "w") as f:
            for dependency in dependencies:
                f.write(f"{dependency}\n")

        pip = ["python3", "-m", "pip", "install", "-r", req_file]
        # Unless we are in a virtualenv, we need --user
        if sys.base_prefix == sys.prefix and not hasattr(sys, "real_prefix"):
            pip.append("--user")

        try:
            subprocess.check_call(pip, stdout=stdout, stderr=stderr)
        except subprocess.CalledProcessError:
            raise internal.Error(_("failed to install dependencies"))

        # Reload sys.path, to find recently installed packages
        importlib.reload(site)


def install_translations(config):
    """Add check translations according to ``config`` as a fallback to existing translations"""

    if not config:
        return

    from . import _translation
    checks_translation = gettext.translation(domain=config["domain"],
                                             localedir=internal.check_dir / config["localedir"],
                                             fallback=True)
    _translation.add_fallback(checks_translation)


def compile_checks(checks, prompt=False):
    # Prompt to replace __init__.py (compile destination)
    if prompt and os.path.exists(internal.check_dir / "__init__.py"):
        if not yes_no_prompt("check50 will compile the YAML checks to __init__.py, are you sure you want to overwrite its contents?"):
            raise Error("Aborting: could not overwrite to __init__.py")

    # Compile simple checks
    with open(internal.check_dir / "__init__.py", "w") as f:
        f.write(simple.compile(checks))

    return "__init__.py"


def setup_logging(level):
    """
    Sets up logging for check50 and lib50.
    level 'info' logs all git commands run to stderr
    level 'debug' logs all git commands and their output to stderr
    """

    for logger in (logging.getLogger("lib50"), LOGGER):
        # Set verbosity level on the lib50 logger
        logger.setLevel(getattr(logging, level.name))

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(ColoredFormatter("(%(levelname)s) %(message)s", use_color=sys.stderr.isatty()))

        # Direct all logs to sys.stderr
        logger.addHandler(handler)

    # Don't animate the progressbar if loglevel is info or debug
    lib50.ProgressBar.DISABLED = level < LogLevel.WARNING


def await_results(commit_hash, slug, pings=45, sleep=2):
    """
    Ping {url} until it returns a results payload, timing out after
    {pings} pings and waiting {sleep} seconds between pings.
    """

    for _i in range(pings):
        # Query for check results.
        res = requests.get(f"https://submit.cs50.io/api/results/check50", params={"commit_hash": commit_hash, "slug": slug})
        results = res.json()

        if res.status_code not in [404, 200]:
            raise RemoteCheckError(results)

        if res.status_code == 200 and results["received_at"] is not None:
            break
        time.sleep(sleep)
    else:
        # Terminate if no response
        raise internal.Error(
            _("check50 is taking longer than normal!\n"
              "See https://submit.cs50.io/check50/{} for more detail").format(commit_hash))

    if not results["check50"]:
        raise RemoteCheckError(results)

    if "error" in results["check50"]:
        raise RemoteCheckError(results["check50"])

    # TODO: Should probably check payload["version"] here to make sure major version is same as __version__
    # (otherwise we may not be able to parse results)
    return results["tag_hash"], {
        "slug": results["check50"]["slug"],
        "results": results["check50"]["results"],
        "version": results["check50"]["version"]
    }


class LogoutAction(argparse.Action):
    """Hook into argparse to allow a logout flag"""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=_("logout of check50")):
        super().__init__(option_strings, dest=dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            lib50.logout()
        except lib50.Error:
            raise internal.Error(_("failed to logout"))
        else:
            termcolor.cprint(_("logged out successfully"), "green")
        parser.exit()


def raise_invalid_slug(slug, offline=False):
    """Raise an error signalling slug is invalid for check50."""
    msg = _("Could not find checks for {}.").format(slug)

    similar_slugs = lib50.get_local_slugs("check50", similar_to=slug)[:3]
    if similar_slugs:
        msg += _(" Did you mean:")
        for similar_slug in similar_slugs:
            msg += f"\n    {similar_slug}"
        msg += _("\nDo refer back to the problem specification if unsure.")

    if offline:
        msg += _("\nIf you are confident the slug is correct and you have an internet connection," \
                " try running without --offline.")

    raise internal.Error(msg)


def validate_args(args):
    """Validate arguments and apply defaults that are dependent on other arguments"""

    # dev implies offline, verbose, and log level "INFO" if not overwritten
    if args.dev:
        args.offline = True
        args.verbose = True
        if "ansi" in args.output:
            args.ansi_output = True

    # offline implies local
    if args.offline:
        args.no_install_dependencies = True
        args.no_download_checks = True
        args.local = True


    args.log_level = LogLevel.__members__[args.log_level.upper()]

    # Setup logging for lib50
    setup_logging(args.log_level)

    # Warning in case of running remotely with no_download_checks or no_install_dependencies set
    if not args.local:
        useless_args = []
        if args.no_download_checks:
            useless_args.append("--no-downloads-checks")
        if args.no_install_dependencies:
            useless_args.append("--no-install-dependencies")

        if useless_args:
            LOGGER.warning(_("You should always use --local when using: {}").format(", ".join(useless_args)))

    # Filter out any duplicates from args.output
    seen_output = []
    for output in args.output:
        if output in seen_output:
            LOGGER.warning(_("Duplicate output format specified: {}").format(output))
        else:
            seen_output.append(output)

    args.output = seen_output

    if "ansi" in args.output:
        if args.log_level <= LogLevel.INFO:
            args.ansi_log = True
    elif args.ansi_log:
        LOGGER.warning(_("--ansi-log has no effect when ansi is not among the output formats"))


def main():
    parser = argparse.ArgumentParser(prog="check50")

    parser.add_argument("slug", help=_("prescribed identifier of work to check"))
    parser.add_argument("-d", "--dev",
                        action="store_true",
                        help=_("run check50 in development mode (implies --offline, --verbose, and --log-level INFO).\n"
                               "causes SLUG to be interpreted as a literal path to a checks package"))
    parser.add_argument("--offline",
                        action="store_true",
                        help=_("run checks completely offline (implies --local, --no-download-checks and --no-install-dependencies)"))
    parser.add_argument("-l", "--local",
                        action="store_true",
                        help=_("run checks locally instead of uploading to cs50"))
    parser.add_argument("-o", "--output",
                        action="store",
                        nargs="+",
                        default=["ansi", "html"],
                        choices=["ansi", "json", "html"],
                        type=str.lower,
                        help=_("format of check results"))
    parser.add_argument("--target",
                        action="store",
                        nargs="+",
                        help=_("target specific checks to run"))
    parser.add_argument("--output-file",
                        action="store",
                        metavar="FILE",
                        help=_("file to write output to"))
    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        help=_("shows the full traceback of any errors and shows print statements written in checks"))
    parser.add_argument("--log-level",
                        action="store",
                        choices=list(LogLevel.__members__),
                        default="WARNING",
                        type=str.upper,
                        help=_("sets the log level."
                               ' "WARNING" (default) displays usage warnings'
                               ' "INFO" shows all commands run.'
                               ' "DEBUG" adds the output of all command run.'))
    parser.add_argument("--ansi-log",
                        action="store_true",
                        help=_("display log in ansi output mode"))
    parser.add_argument("--no-download-checks",
                        action="store_true",
                        help=_("do not download checks, but use previously downloaded checks instead (only works with --local)"))
    parser.add_argument("--no-install-dependencies",
                        action="store_true",
                        help=_("do not install dependencies (only works with --local)"))
    parser.add_argument("-V", "--version",
                        action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--logout", action=LogoutAction)

    args = parser.parse_args()

    # Validate arguments and apply defaults
    validate_args(args)

    # Set excepthook
    excepthook.verbose = args.verbose
    excepthook.outputs = args.output
    excepthook.output_file = args.output_file
    excepthook.slug = args.slug

    # If remote, push files to GitHub and await results
    if not args.local:
        commit_hash = lib50.push("check50", args.slug, internal.CONFIG_LOADER, data={"check50": True})[1]
        with lib50.ProgressBar("Waiting for results") if "ansi" in args.output else nullcontext():
            tag_hash, results = await_results(commit_hash, args.slug)
    # Otherwise run checks locally
    else:
        with lib50.ProgressBar("Checking") if "ansi" in args.output else nullcontext():
            # If developing, assume slug is a path to check_dir
            if args.dev:
                internal.check_dir = Path(args.slug).expanduser().resolve()
                if not internal.check_dir.is_dir():
                    raise internal.Error(_("{} is not a directory").format(internal.check_dir))
            # Otherwise have lib50 create a local copy of slug
            else:
                try:
                    internal.check_dir = lib50.local(args.slug, offline=args.no_download_checks)
                except lib50.ConnectionError:
                    raise internal.Error(_("check50 could not retrieve checks from GitHub. Try running check50 again with --offline.").format())
                except lib50.InvalidSlugError:
                    raise_invalid_slug(args.slug, offline=args.no_download_checks)

            # Load config
            config = internal.load_config(internal.check_dir)

            # Compile local checks if necessary
            if isinstance(config["checks"], dict):
                config["checks"] = internal.compile_checks(config["checks"], prompt=args.dev)

            install_translations(config["translations"])

            if not args.no_install_dependencies:
                install_dependencies(config["dependencies"], verbose=args.verbose)

            checks_file = (internal.check_dir / config["checks"]).resolve()

            # Have lib50 decide which files to include
            included = lib50.files(config.get("files"))[0]

            # Redirect stdout/stderr to devnull if not verbose
            with nullcontext() if args.verbose else open(os.devnull, "w") as devnull:
                if args.verbose:
                    stdout = sys.stdout
                    stderr = sys.stderr
                else:
                    stdout = stderr = devnull

                # Create a working_area (temp dir) named - with all included student files
                with lib50.working_area(included, name='-') as working_area, \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):

                    check_results = CheckRunner(checks_file).run(included, working_area, args.target)
                    results = {
                        "slug": args.slug,
                        "results": [attr.asdict(result) for result in check_results],
                        "version": __version__
                    }

    # Log results in debug
    LOGGER.debug(results)

    # Render output
    file_manager = open(args.output_file, "w") if args.output_file else nullcontext(sys.stdout)
    with file_manager as output_file:
        for output in args.output:
            if output == "json":
                output_file.write(renderer.to_json(**results))
                output_file.write("\n")
            elif output == "ansi":
                output_file.write(renderer.to_ansi(**results, _log=args.ansi_log))
                output_file.write("\n")
            elif output == "html":
                if os.environ.get("CS50_IDE_TYPE") and args.local:
                    html = renderer.to_html(**results)
                    subprocess.check_call(["c9", "exec", "renderresults", "check50", html])
                else:
                    if args.local:
                        html = renderer.to_html(**results)
                        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".html") as html_file:
                            html_file.write(html)
                        url = f"file://{html_file.name}"
                    else:
                        url = f"https://submit.cs50.io/check50/{tag_hash}"

                    termcolor.cprint(_("To see the results in your browser go to {}").format(url), "white", attrs=["bold"])


if __name__ == "__main__":
    main()
