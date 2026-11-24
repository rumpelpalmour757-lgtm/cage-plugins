#!/usr/bin/env python3
"""Check every plugin, pack each into dist/, and write index.json.

Run it after adding or changing a plugin. With --check it changes nothing and
fails if index.json or dist/ would differ, which is what the pull request
check runs.
"""
import gzip
import hashlib
import io
import json
import pathlib
import re
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://raw.githubusercontent.com/Lee-xeo/cage-plugins/main/"
FIELDS = {"name", "displayName", "version", "description", "engines", "kind", "platforms", "permissions",
          "categories", "activationEvents", "command", "icon", "contributes"}
TARGETS = {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-arm64", "win32-x64"}
ENGINE = re.compile(r"^(>=|\^)\d+\.\d+\.\d+$")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")
COMMON_SCHEMES = {"http", "https", "file", "ftp", "mailto", "data", "javascript", "blob", "about", "chrome", "ws", "wss", "claude", "tailscale"}
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
PORT = re.compile(r"^(\*|\d{1,5})$")
PERMISSIONS = {"files", "commands", "screen", "input", "network"}
# The market's sections. The application shows only those it knows, so add
# here only after the application knows the new one.
CATEGORIES = ("desktop", "developer", "system", "network", "files", "productivity", "example")
PLATFORMS = {"darwin", "linux", "windows"}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,2}([-+.][0-9A-Za-z.-]+)?$")
# Tools describe only what they do, never where the model runs. Latin words
# are matched whole, as the application does.
RESERVED = ("cage", "guest", "vm", "virtual machine", "虚拟机", "客机")
MAX_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
MAX_ICON_BYTES = 256 * 1024
MAX_I18N_BYTES = 256 * 1024
OFFICIAL_AUTHORS = {"Lee-xeo"}
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
# The interface languages Cage offers, matching internal/uilocale/languages.json.
REQUIRED_LOCALES = (
    "zh-CN", "zh-TW", "ru", "ar", "fa", "ko", "es", "my",
    "be", "ps", "prs", "am", "so", "ti", "fr",
)
# Desktop applications: the vendor's own source, never a pinned version.
DESKTOP_APPS_SINCE = (0, 3, 2)
DEB_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$")
APT_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,62}$")
FINGERPRINT = re.compile(r"^[0-9A-F]{40}$")
LOCALE_TAG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
I18N_FIELDS = {"displayName", "description"}


def fail(message):
    sys.exit("error: " + message)


def inside(folder, name, suffixes):
    """Whether name is a path of a file inside the plugin with one of the suffixes."""
    path = pathlib.PurePosixPath(str(name))
    return (isinstance(name, str) and name and not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and path.as_posix() == name and path.suffix.lower() in suffixes
            and (folder / name).is_file() and not (folder / name).is_symlink())


def only(where, value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        fail(f"{where}: must be an object with fields drawn from {sorted(allowed)}")


NPM_PINNED = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
PYPI_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==\d+(\.\d+)*([-+.][0-9A-Za-z.]+)?$")
CUTOFF = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?$")


def command_ok(command):
    return isinstance(command, list) and command and all(isinstance(part, str) and part for part in command)


def fetched_ok(command):
    """A command that has npx or uv fetch a package names one exact version,
    so every start runs the same published files: npm and PyPI never change
    the files of a version once published. uv also takes --exclude-newer, so
    the packages it depends on resolve as they did on that day."""
    program = command[0]
    if program == "npx":
        packages = [part for part in command[1:] if not part.startswith("-")][:1]
        return "--yes" in command and bool(packages) and bool(NPM_PINNED.match(packages[0]))
    if program in ("uv", "uvx"):
        if program == "uv" and command[1:3] != ["run", "--no-project"]:
            return False
        pairs = list(zip(command, command[1:]))
        wanted = [value for flag, value in pairs if flag in ("--from", "--with")]
        cutoffs = [value for flag, value in pairs if flag == "--exclude-newer"]
        return (bool(wanted) and all(PYPI_PINNED.match(value) for value in wanted)
                and len(cutoffs) == 1 and bool(CUTOFF.match(cutoffs[0])))
    return program not in ("bunx", "pipx", "pnpx", "pnpm", "yarn", "npm", "pip", "pip3")


SOURCE_FIELDS = {"project", "version", "license", "url"}
LICENSE = re.compile(r"^[A-Za-z0-9.+-]+( (AND|OR|WITH) [A-Za-z0-9.+-]+)*$")


HEX64 = re.compile(r"^[0-9a-f]{64}$")
WHEEL_HOST = "https://files.pythonhosted.org/"


def check_wording(where, text):
    lowered = text.lower()
    words = set(re.split(r"[^a-z0-9\u0080-\U0010ffff]+", lowered))
    for word in RESERVED:
        if (word in lowered) if (" " in word or not word.isascii()) else (word in words):
            fail(f"{where} mentions {word!r}")


def check_tools_file(folder):
    """If a plugin ships tools.json, check that none of its tool descriptions leak reserved words."""
    path = folder / "tools.json"
    if not path.is_file():
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    tools = raw["tools"] if isinstance(raw, dict) and isinstance(raw.get("tools"), list) else raw
    if not isinstance(tools, list) or not tools:
        fail(f"{folder.name}: tools.json must contain a non-empty list of tools")
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            fail(f"{folder.name}: tools.json has an invalid tool entry")
        parts = [tool.get("name", ""), tool.get("title", ""), tool.get("description", "")]
        if isinstance(tool.get("annotations"), dict):
            parts.append(str(tool["annotations"].get("title", "")))
        for key in ("inputSchema", "outputSchema"):
            if tool.get(key) is not None:
                parts.append(json.dumps(tool[key], ensure_ascii=False))
        check_wording(f"{folder.name}: tools.json tool {tool['name']}", " ".join(parts))


def check_launch(folder, data):
    """Check a launcher-backed plugin's launch.json, launcher.py, lock file and tools.json."""
    path = folder / "launch.json"
    if not path.is_file():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    only(f"{folder.name}: launch.json", config, {"name", "version", "server", "python", "env", "requires", "dataDirs", "toolPrefix"})
    if "toolPrefix" in config and not re.match(r"^[a-z][a-z0-9_]*_$", str(config["toolPrefix"])):
        fail(f"{folder.name}: launch.json toolPrefix must be lowercase letters, digits and underscores ending with _")
    if config.get("name") != data["name"] or config.get("version") != data["version"]:
        fail(f"{folder.name}: launch.json name and version must match plugin.json")
    launcher = folder / "launcher.py"
    canonical = ROOT / "scripts" / "launcher.py"
    if not launcher.is_file() or launcher.read_bytes() != canonical.read_bytes():
        fail(f"{folder.name}: launcher.py must match scripts/launcher.py")
    if not (folder / "tools.json").is_file():
        fail(f"{folder.name}: launch.json requires tools.json")
    server = config.get("server")
    if not isinstance(server, dict):
        fail(f"{folder.name}: launch.json server must be an object")
    if "entry" in server:
        only(f"{folder.name}: launch.json server", server, {"entry", "args"})
        if not isinstance(server["entry"], str) or ":" not in server["entry"]:
            fail(f"{folder.name}: launch.json server.entry must look like module:attr")
        if "args" in server and not (isinstance(server["args"], list) and all(isinstance(x, str) for x in server["args"])):
            fail(f"{folder.name}: launch.json server.args must be a list of strings")
        python = config.get("python")
        if not isinstance(python, dict):
            fail(f"{folder.name}: launch.json server.entry requires a python lock section")
        only(f"{folder.name}: launch.json python", python, {"requires", "pythons", "resolvedBefore", "lock"})
        reqs = python.get("requires")
        if not isinstance(reqs, list) or not reqs or not all(isinstance(r, str) and PYPI_PINNED.match(r) for r in reqs):
            fail(f"{folder.name}: launch.json python.requires must pin exact package==version entries")
        if "resolvedBefore" in python and not CUTOFF.match(str(python["resolvedBefore"])):
            fail(f"{folder.name}: launch.json python.resolvedBefore must be an ISO date")
        lock_name = python.get("lock")
        if not inside(folder, lock_name, (".json",)):
            fail(f"{folder.name}: launch.json python.lock must point to a JSON file inside the plugin")
        lock = json.loads((folder / lock_name).read_text(encoding="utf-8"))
        if lock.get("requires") != reqs:
            fail(f"{folder.name}: {lock_name} requires does not match launch.json python.requires")
        envs = lock.get("environments")
        if not isinstance(envs, dict) or not envs:
            fail(f"{folder.name}: {lock_name} environments must be a non-empty object")
        for env_key, items in envs.items():
            if not isinstance(items, list) or not items:
                fail(f"{folder.name}: {lock_name} environment {env_key} must be a non-empty list")
            for item in items:
                url = str(item.get("url", ""))
                if not url.startswith(WHEEL_HOST) or not str(item.get("file", "")).endswith(".whl"):
                    fail(f"{folder.name}: {lock_name} {env_key}: wheel must come from {WHEEL_HOST}")
                if not isinstance(item.get("size"), int) or item["size"] <= 0 or not HEX64.match(str(item.get("sha256", ""))):
                    fail(f"{folder.name}: {lock_name} {env_key}: wheel {item.get('file')!r} needs size and sha256")
    elif "command" in server:
        only(f"{folder.name}: launch.json server", server, {"command"})
        if not command_ok(server["command"]) or not fetched_ok(server["command"]):
            fail(f"{folder.name}: launch.json server.command is invalid or fetches an unpinned package")
        if server["command"][0] == "node" and (folder / "package.json").is_file():
            if not (folder / "package-lock.json").is_file() or not (folder / "node_modules").is_dir():
                fail(f"{folder.name}: a Node.js launcher plugin must include package-lock.json and vendored node_modules")
    else:
        fail(f"{folder.name}: launch.json server needs entry or command")
    return config


def runtime_of(folder, data, launch_config):
    """The runtime program a plugin needs, for the market's index."""
    if launch_config:
        if "python" in launch_config:
            return "python3"
        program = launch_config["server"]["command"][0]
    else:
        servers = data.get("contributes", {}).get("mcpServers", [])
        command = servers[0]["command"] if servers else data.get("command", [])
        if not command:
            return ""
        program = command[0]
    base = pathlib.PurePosixPath(program.replace("\\", "/")).name.lower()
    if base in ("powershell.exe", "powershell", "pwsh"):
        return "powershell"
    if base in ("uv", "uvx"):
        return "uv"
    if base == "npx":
        return "npx"
    if base == "node":
        return "node"
    if base in ("python3", "python"):
        return "python3"
    return ""


def source(folder):
    """The open-source project a plugin builds on, from its source.json. It
    stays out of plugin.json, which older applications read strictly."""
    path = folder / "source.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    only(f"{folder.name}: source.json", data, SOURCE_FIELDS)
    plain = lambda text, limit: isinstance(text, str) and len(text) <= limit and not re.search(r"[\x00-\x1f\x7f]", text)
    if not plain(data.get("project"), 100) or not data["project"].strip():
        fail(f"{folder.name}: source.json needs a project")
    if not plain(data.get("version", ""), 40) or not LICENSE.match(str(data.get("license", "MIT"))):
        fail(f"{folder.name}: source.json version must be short and license an SPDX expression")
    if "url" in data and not (plain(data["url"], 300) and re.match(r"^https://[a-z0-9.-]+\.[a-z]{2,}(/[^\s]*)?$", data["url"])):
        fail(f"{folder.name}: source.json url must be https")
    if data.get("license") and not (folder / "LICENSE").is_file():
        fail(f"{folder.name}: a plugin built on another project keeps that project's LICENSE beside it")
    return data


def load_authors(plugin_names):
    """Plugin attribution from authors.json: either an official maintainer or
    the contributor's GitHub username. Kept out of plugin.json so older apps
    read manifests strictly and contributors cannot self-declare official."""
    path = ROOT / "authors.json"
    if not path.is_file() or path.is_symlink():
        fail("authors.json is missing at the repository root")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("authors.json must be an object mapping plugin name to GitHub username")
    missing = sorted(set(plugin_names) - set(data))
    if missing:
        fail(f"authors.json is missing entries for: {', '.join(missing)}")
    extra = sorted(set(data) - set(plugin_names))
    if extra:
        fail(f"authors.json lists unknown plugins: {', '.join(extra)}")
    out = {}
    for name, login in data.items():
        if not isinstance(login, str) or not GITHUB_LOGIN.match(login):
            fail(f"authors.json {name}: author must be a valid GitHub username")
        entry = {"name": login}
        if login in OFFICIAL_AUTHORS:
            entry["official"] = True
        out[name] = entry
    return out


def load_i18n(folder, target_name):
    """Localized displayName and description from i18n.json inside the plugin.
    Kept out of plugin.json so older apps read manifests strictly; packed into
    each archive and published in index.json so Cage displays the plugin in its
    active interface language."""
    path = folder / "i18n.json"
    if not path.is_file() or path.is_symlink():
        fail(f"{folder.name}: i18n.json is missing; provide translations for all {len(REQUIRED_LOCALES)} Cage languages")
    if path.stat().st_size > MAX_I18N_BYTES:
        fail(f"{folder.name}: i18n.json is over {MAX_I18N_BYTES // 1024} KB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail(f"{folder.name}: i18n.json must be an object keyed by language code")
    missing = [code for code in REQUIRED_LOCALES if code not in data]
    if missing:
        fail(f"{folder.name}: i18n.json is missing required Cage locales: {', '.join(missing)}")
    plain = lambda text, limit: isinstance(text, str) and bool(text.strip()) and len(text) <= limit and not re.search(r"[\x00-\x1f\x7f]", text)
    normalized = {}
    for code, entry in data.items():
        if not isinstance(code, str) or not LOCALE_TAG.match(code):
            fail(f"{folder.name}: i18n.json has invalid language code {code!r}")
        only(f"{folder.name}: i18n.json[{code}]", entry, I18N_FIELDS)
        if not plain(entry.get("description"), 600):
            fail(f"{folder.name}: i18n.json[{code}].description must be non-empty plain text (<= 600 chars)")
        item = {}
        if "displayName" in entry:
            if not plain(entry["displayName"], 80):
                fail(f"{folder.name}: i18n.json[{code}].displayName must be non-empty plain text (<= 80 chars)")
            item["displayName"] = entry["displayName"].strip()
        item["description"] = entry["description"].strip()
        check_wording(f"{folder.name}: i18n.json[{code}]", json.dumps(item, ensure_ascii=False))
        normalized[code] = item
    if target_name:
        base = ROOT / "plugins" / folder.name / "i18n.json"
        if base.is_file() and base.read_bytes() != path.read_bytes():
            fail(f"targets/{target_name}/{folder.name}: i18n.json must match plugins/{folder.name}/i18n.json")
    return normalized


def open_url_ok(pattern):
    scheme, found, rest = str(pattern).partition("://")
    authority, slash, route = rest.partition("/")
    if not found or not slash or re.search(r"[\s?#@\\]", pattern):
        return False
    host, colon, port = authority.partition(":")
    if colon and not PORT.match(port):
        return False
    if scheme == "https":
        if not HOST.match(host):
            return False
    elif scheme != "http" or host not in ("127.0.0.1", "localhost"):
        return False
    return "*" not in route.removesuffix("*")


def contributes(folder, data):
    """Check the second manifest format: what the plugin adds."""
    name, c = folder.name, data["contributes"]
    only(f"{name}: contributes", c, {"mcpServers", "views", "uriHandlers", "desktopApps"})
    if "command" in data:
        fail(f"{name}: command belongs in contributes.mcpServers")
    engines = data.get("engines")
    if not isinstance(engines, dict) or set(engines) != {"cage"} or not ENGINE.match(str(engines["cage"])):
        fail(f"{name}: engines.cage must be >=X.Y.Z or ^X.Y.Z")
    if data.get("kind") not in ("host", "guest"):
        fail(f"{name}: kind must be host or guest")
    if data["kind"] == "guest" and data.get("platforms"):
        fail(f"{name}: platforms is for host plugins")
    servers, views, schemes = [], [], []
    for server in c.get("mcpServers", []):
        only(f"{name}: mcpServers", server, {"id", "command"})
        if not NAME.match(str(server.get("id", ""))) or server["id"] in servers or not command_ok(server.get("command")):
            fail(f"{name}: mcpServers needs a unique id and a command")
        if not fetched_ok(server["command"]):
            fail(f"{name}: mcpServers {server['id']}: a package fetched at start must name one exact version "
                 "(npx --yes pkg@1.2.3, or uv with --from/--with pkg==1.2.3 and --exclude-newer)")
        servers.append(server["id"])
    for view in c.get("views", []):
        only(f"{name}: views", view, {"id", "title", "icon", "entry", "server", "openUrls", "presentation", "width", "height"})
        if not NAME.match(str(view.get("id", ""))) or view["id"] in views:
            fail(f"{name}: views needs a unique id")
        if not isinstance(view.get("title"), str) or not 0 < len(view["title"]) <= 80:
            fail(f"{name}: view {view['id']} needs a title")
        if not inside(folder, view.get("entry"), (".html",)):
            fail(f"{name}: view {view['id']}: entry must be an HTML file inside the plugin")
        if "icon" in view and not inside(folder, view["icon"], (".png", ".svg")):
            fail(f"{name}: view {view['id']}: icon must be a PNG or SVG file inside the plugin")
        if "server" in view and view["server"] not in servers:
            fail(f"{name}: view {view['id']}: server is not one of mcpServers")
        presentation = view.get("presentation")
        if presentation is not None and presentation not in ("page", "floating"):
            fail(f"{name}: view {view['id']}: presentation must be 'page' or 'floating'")
        for dim, lo, hi in (("width", 220, 720), ("height", 100, 720)):
            if dim in view:
                if presentation != "floating":
                    fail(f"{name}: view {view['id']}: {dim} is only allowed when presentation is 'floating'")
                if not isinstance(view[dim], int) or isinstance(view[dim], bool) or not (lo <= view[dim] <= hi):
                    fail(f"{name}: view {view['id']}: {dim} must be an integer between {lo} and {hi}")
        if any(k in view for k in ("presentation", "width", "height")):
            m = re.match(r"^(?:>=|\^)(\d+)\.(\d+)\.(\d+)$", str(engines["cage"]))
            if not m or tuple(int(p) for p in m.groups()) < (0, 2, 0):
                fail(f"{name}: view {view['id']}: presentation needs engines.cage >= 0.2.0")
        for pattern in view.get("openUrls", []):
            if not open_url_ok(pattern):
                fail(f"{name}: view {view['id']}: openUrls {pattern!r} must be https to a named host, or http to "
                     "127.0.0.1 or localhost, with a path that may end in *")
        views.append(view["id"])
    for handler in c.get("uriHandlers", []):
        only(f"{name}: uriHandlers", handler, {"scheme", "server", "tool", "reveal"})
        scheme = str(handler.get("scheme", ""))
        if not SCHEME.match(scheme) or len(scheme) > 40 or scheme in COMMON_SCHEMES or scheme in schemes:
            fail(f"{name}: uriHandlers scheme {scheme!r} is not valid, reserved or repeated")
        if handler.get("server") not in servers or not handler.get("tool"):
            fail(f"{name}: uriHandlers {scheme}: server must be one of mcpServers, and tool is required")
        if "reveal" in handler and handler["reveal"] not in views:
            fail(f"{name}: uriHandlers {scheme}: reveal is not one of views")
        schemes.append(scheme)
    if c.get("desktopApps"):
        desktop_apps(folder, data, schemes)
    for event in data.get("activationEvents", []):
        kind, _, target = str(event).partition(":")
        if not (event == "onStartupFinished" or kind == "onView" and target in views or kind == "onUri" and target in schemes):
            fail(f"{name}: unknown activation event {event!r}")


def https_ok(address):
    """Whether address is https to a named host on the default port."""
    if not isinstance(address, str) or len(address) > 400 or not address.startswith("https://"):
        return False
    rest = address[len("https://"):]
    host = rest.split("/", 1)[0]
    return HOST.match(host) is not None and not re.search(r"[\s?#@\\]", rest)


def plain(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and not re.search(r"[\x00\r\n]", value)


def desktop_apps(folder, data, schemes):
    """Check desktop applications, as the application does: a guest plugin
    names the vendor's source and how to start the app, never a version."""
    name, apps = folder.name, data["contributes"]["desktopApps"]
    if data["kind"] != "guest":
        fail(f"{name}: desktopApps are for guest plugins")
    floor = tuple(int(part) for part in re.sub(r"^(>=|\^)", "", data["engines"]["cage"]).split("."))
    if floor < DESKTOP_APPS_SINCE:
        fail(f"{name}: desktopApps need engines.cage of at least {'.'.join(map(str, DESKTOP_APPS_SINCE))}")
    if not isinstance(apps, list) or not 0 < len(apps) <= 4:
        fail(f"{name}: desktopApps must list one to four applications")
    ids, taken = [], list(schemes)
    for app in apps:
        only(f"{name}: desktopApps", app, {"id", "title", "icon", "source", "command", "schemes", "packages"})
        app_id = str(app.get("id", ""))
        where = f"{name}: desktopApps {app_id}"
        if not NAME.match(app_id) or len(app_id) > 40 or app_id in ids:
            fail(f"{name}: desktopApps needs a unique id")
        if not isinstance(app.get("title"), str) or not 0 < len(app["title"].strip()) <= 80:
            fail(f"{where}: needs a title")
        if "icon" in app and not inside(folder, app["icon"], (".png", ".svg")):
            fail(f"{where}: icon must be a PNG or SVG file inside the plugin")
        source = app.get("source")
        if not isinstance(source, dict):
            fail(f"{where}: source is missing")
        command = app.get("command")
        if not isinstance(command, list) or not 0 < len(command) <= 16 or not all(plain(part) for part in command):
            fail(f"{where}: command is missing or not valid")
        if source.get("type") == "apt":
            only(f"{where}: source", source, {"type", "repository", "suite", "components", "key", "fingerprint", "package"})
            components = source.get("components")
            if not (https_ok(source.get("repository")) and https_ok(source.get("key"))
                    and APT_WORD.match(str(source.get("suite", ""))) and FINGERPRINT.match(str(source.get("fingerprint", "")))
                    and DEB_PACKAGE.match(str(source.get("package", "")))
                    and isinstance(components, list) and 0 < len(components) <= 4
                    and all(APT_WORD.match(str(part)) and "/" not in str(part) for part in components)):
                fail(f"{where}: an apt source needs an https repository and key, a suite, components, "
                     "the key's full uppercase fingerprint and a package")
        elif source.get("type") in ("deb", "archive"):
            only(f"{where}: source", source, {"type", "resolve", "hosts"})
            resolve, hosts = source.get("resolve"), source.get("hosts")
            if (not isinstance(resolve, list) or not 0 < len(resolve) <= 8 or not inside(folder, resolve[0], (".py", ".sh"))
                    or not all(plain(part) for part in resolve[1:])):
                fail(f"{where}: source.resolve must start with a self-contained .py or .sh file inside the plugin")
            if not isinstance(hosts, list) or not 0 < len(hosts) <= 8 or not all(HOST.match(str(host)) for host in hosts):
                fail(f"{where}: source.hosts must list one to eight download hosts")
        else:
            fail(f"{where}: source.type must be apt, deb or archive")
        program = command[0]
        path = pathlib.PurePosixPath(program)
        if source["type"] == "archive":
            if path.is_absolute() or ".." in path.parts or "\\" in program or path.as_posix() != program:
                fail(f"{where}: command must start with a program inside the release")
        elif not (program.startswith("/usr/") or program.startswith("/opt/")) or "/../" in program or program.endswith("/"):
            fail(f"{where}: command must start with an absolute path under /usr or /opt")
        app_schemes = app.get("schemes", [])
        if not isinstance(app_schemes, list) or len(app_schemes) > 4:
            fail(f"{where}: at most four schemes")
        for scheme in app_schemes:
            scheme = str(scheme)
            if not SCHEME.match(scheme) or len(scheme) > 40 or scheme in COMMON_SCHEMES or scheme == "x-scheme-handler" or scheme in taken:
                fail(f"{where}: scheme {scheme!r} is not valid, reserved or repeated")
            taken.append(scheme)
        packages = app.get("packages", [])
        if not isinstance(packages, list) or len(packages) > 40 or not all(DEB_PACKAGE.match(str(p)) for p in packages):
            fail(f"{where}: packages must be Debian package names")
        ids.append(app_id)


def manifest(folder):
    path = folder / "plugin.json"
    if not path.is_file():
        fail(f"{folder.name}: plugin.json is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    extra = set(data) - FIELDS
    if extra:
        fail(f"{folder.name}: unknown fields {sorted(extra)}")
    if data.get("name") != folder.name or not NAME.match(folder.name) or len(folder.name) > 40:
        fail(f"{folder.name}: name must equal the folder name: lowercase letters, digits and hyphens")
    if not VERSION.match(str(data.get("version", ""))):
        fail(f"{folder.name}: version must look like 1.2.3")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        fail(f"{folder.name}: description is missing")
    if not isinstance(data.get("permissions"), list) or set(data["permissions"]) - PERMISSIONS:
        fail(f"{folder.name}: permissions must be a list drawn from {sorted(PERMISSIONS)}")
    if set(data.get("platforms", [])) - PLATFORMS:
        fail(f"{folder.name}: platforms must be drawn from {sorted(PLATFORMS)}")
    if "displayName" in data and (not isinstance(data["displayName"], str) or not 0 < len(data["displayName"]) <= 80):
        fail(f"{folder.name}: displayName must be a short name")
    categories = data.get("categories", [])
    if (not isinstance(categories, list) or not 0 < len(categories) <= 3 or len(set(categories)) != len(categories)
            or set(categories) - set(CATEGORIES)):
        fail(f"{folder.name}: categories must name one to three of {list(CATEGORIES)}")
    if "contributes" in data:
        contributes(folder, data)
    else:
        # The first format: one command, and nothing the second format added.
        if not command_ok(data.get("command")) or not fetched_ok(data["command"]):
            fail(f"{folder.name}: command is missing, or fetches a package without an exact version")
        if data.get("kind", "host") != "host" or "engines" in data or "activationEvents" in data:
            fail(f"{folder.name}: kind, engines and activationEvents need contributes")
    icon = data.get("icon")
    if icon is not None:
        if not inside(folder, icon, (".png", ".svg")):
            fail(f"{folder.name}: icon must be a PNG or SVG file inside the plugin")
        if (folder / icon).stat().st_size > MAX_ICON_BYTES:
            fail(f"{folder.name}: icon is over {MAX_ICON_BYTES // 1024} KB")
    # Only what is shown to people and models; kind and engines are structure.
    shown = [data["name"], data.get("displayName", ""), data["description"]]
    shown += [view["title"] for view in data.get("contributes", {}).get("views", [])]
    shown += [app["title"] for app in data.get("contributes", {}).get("desktopApps", [])]
    check_wording(f"{folder.name}: the manifest", json.dumps(shown, ensure_ascii=False))
    check_tools_file(folder)
    return data


def pack(folder):
    """A tar.gz whose bytes depend only on the files' paths and contents."""
    raw = io.BytesIO()
    file_count, unpacked = 0, 0
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder).as_posix()
            if path.is_symlink():
                fail(f"{folder.name}: {relative} is a link; plugins contain only files and folders")
            if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(folder).parts):
                continue
            info = tarfile.TarInfo(folder.name + "/" + relative)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                file_count += 1
                unpacked += len(data)
                if file_count > MAX_FILES or unpacked > MAX_UNPACKED_BYTES:
                    fail(f"{folder.name}: unpacked plugin exceeds {MAX_FILES} files or {MAX_UNPACKED_BYTES // 1024 // 1024} MB")
                info.size, info.mode = len(data), 0o755
                archive.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0, filename="") as out:
        out.write(raw.getvalue())
    if len(packed.getvalue()) > MAX_BYTES:
        fail(f"{folder.name}: the packed plugin is over {MAX_BYTES // 1024 // 1024} MB")
    return packed.getvalue()


def folders():
    """Each plugin folder with its target: plugins/<name> runs anywhere,
    targets/<target>/<name> holds a build for one system and processor."""
    found = [(folder, "") for folder in sorted((ROOT / "plugins").iterdir()) if folder.is_dir()]
    for group in sorted((ROOT / "targets").iterdir()) if (ROOT / "targets").is_dir() else []:
        if not group.is_dir():
            continue
        if group.name not in TARGETS:
            fail(f"targets/{group.name}: target must be one of {sorted(TARGETS)}")
        found += [(folder, group.name) for folder in sorted(group.iterdir()) if folder.is_dir()]
    return found


def main():
    check = "--check" in sys.argv[1:]
    plugin_folders = folders()
    authors = load_authors({folder.name for folder, _ in plugin_folders})
    entries, wanted, seen_tools = [], {}, {}
    for folder, target_name in plugin_folders:
        data = manifest(folder)
        i18n = load_i18n(folder, target_name)
        launch_config = check_launch(folder, data)
        if not target_name and (folder / "tools.json").is_file() and data["name"] != "linux-desktop":
            raw = json.loads((folder / "tools.json").read_text(encoding="utf-8"))
            tools = raw["tools"] if isinstance(raw, dict) and isinstance(raw.get("tools"), list) else raw
            for tool in tools:
                tname = tool["name"]
                if tname in seen_tools:
                    fail(f"{folder.name}: tool name {tname!r} collides with plugin {seen_tools[tname]!r}")
                seen_tools[tname] = folder.name
        name = f"{data['name']}-{data['version']}{'-' + target_name if target_name else ''}.tar.gz"
        target = ROOT / "dist" / name
        packed = pack(folder)
        if target.exists() and target.read_bytes() != packed:
            fail(f"{folder.name}: its files changed but its version did not; raise the version in plugin.json")
        wanted[target] = packed
        entry = {key: data[key] for key in ("name", "version", "description", "permissions")}
        entry["author"] = authors[data["name"]]
        entry["i18n"] = i18n
        for key in ("displayName", "platforms", "categories", "kind", "engines", "contributes"):
            if data.get(key):
                entry[key] = data[key]
        if target_name:
            entry["target"] = target_name
        runtime = runtime_of(folder, data, launch_config)
        if runtime:
            entry["runtime"] = runtime
        upstream = source(folder)
        if upstream:
            entry["source"] = upstream
        # The application takes pictures only from plugins/<name>/; a build
        # for one target shows the picture of the plugin's folder there.
        if data.get("icon") and (not target_name or (ROOT / "plugins" / data["name"] / data["icon"]).is_file()):
            entry["icon"] = BASE + "plugins/" + data["name"] + "/" + data["icon"]
        entry["archive"] = BASE + "dist/" + name
        entry["sha256"] = hashlib.sha256(packed).hexdigest()
        entry["bytes"] = len(packed)
        entries.append(entry)
    index = json.dumps({"version": 1, "plugins": entries}, ensure_ascii=False, indent=2) + "\n"
    path = ROOT / "index.json"
    if check:
        stale = [str(target.relative_to(ROOT)) for target in wanted if not target.exists()]
        if stale or not path.exists() or path.read_text(encoding="utf-8") != index:
            fail("index.json or dist/ is out of date; run python3 scripts/build.py and commit the result")
        print(f"ok: {len(entries)} plugins")
        return
    (ROOT / "dist").mkdir(exist_ok=True)
    for target, packed in wanted.items():
        if not target.exists():
            target.write_bytes(packed)
    path.write_text(index, encoding="utf-8")
    print(f"wrote index.json with {len(entries)} plugins")


if __name__ == "__main__":
    main()
