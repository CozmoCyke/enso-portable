import threading, uuid, platform, logging, os, random, string, json, mimetypes, sys
from pathlib import Path
import enso.messages

import enso
from enso import config
from enso.contrib import retreat
from enso.quasimode import layout
from enso.commands.manager import CommandManager
from enso.contrib.scriptotron.tracker import ScriptTracker
from enso import settings_registry

from flask import Flask, request, send_from_directory, abort
from functools import wraps
from werkzeug.serving import make_server

VAI_ROOT = Path(__file__).resolve().parents[2] / "V-AI"
if str(VAI_ROOT) not in sys.path:
    sys.path.insert(0, str(VAI_ROOT))

from vit.render import render_vit_json, render_vit_page
from vit.runtime import load_latest_payload

HOST = "localhost"
PORT = 31750
AUTH_TOKEN = str(uuid.uuid4())

webui_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(webui_dir, "webui")

app = Flask(__name__, static_url_path='', static_folder=None)

#app.debug = True

config.WEBUI_APP = app

log = logging.getLogger('werkzeug')
log.disabled = True # False
app.logger.disabled = True # False


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if config.ENABLE_WEB_UI_CSRF and (not request.authorization or request.authorization["password"] != AUTH_TOKEN):
            return abort(401)
        return f(*args, **kwargs)
    return decorated


@app.route('/vit')
@app.route('/vit.html')
@app.route('/ui/vit')
@app.route('/ui/vit.html')
@requires_auth
def vit_page():
    return render_vit_page(load_latest_payload())


@app.route('/vit/json')
@app.route('/vit.json')
@app.route('/ui/vit/json')
@app.route('/ui/vit.json')
@requires_auth
def vit_json_page():
    return render_vit_json(load_latest_payload())


@app.route('/<path:filename>')
def my_static(filename):
    if filename.endswith(".html"):
        return inject_enso_token(filename)
    else:
        return send_from_directory("webui", filename)


def inject_enso_token(filename):
    filename = os.path.join(app.root_path, "webui", filename)

    with open(filename, encoding="utf-8") as file:
        content = file.read()
        content = content.replace("%%ENSO_TOKEN%%", AUTH_TOKEN)
        return content


@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers['Cache-Control'] = 'public, max-age=0'
    return r


@app.route('/api/python/version')
@requires_auth
def get_python_version():
    return platform.python_version()


@app.route('/api/enso/version')
@requires_auth
def get_enso_version():
    return config.ENSO_VERSION


@app.route('/api/retreat/installed')
@requires_auth
def get_retreat_installed():
    if retreat.installed():
        return "True"
    return ""


@app.route('/api/retreat/show_options')
@requires_auth
def get_retreat_show_settings():
    retreat.options()
    return ""


@app.route('/api/enso/color_themes')
@requires_auth
def get_enso_themes():
    return json.dumps({"current": config.COLOR_THEME, "all": layout.COLOR_THEMES})


@app.route('/api/enso/get/config/<key>')
@requires_auth
def get_enso_get_config(key):
    config_vars = vars(config)
    if key in config_vars:
        return str(config_vars[key])
    else:
        return ""


@app.route('/api/enso/set/config/<key>/<value>')
@requires_auth
def get_enso_set_config(key, value):
    key = key.upper()
    if key == "DISABLED_COMMANDS":
        return abort(400)
    if not settings_registry.is_known_setting(key) and key not in vars(config):
        return abort(400)
    if settings_registry.is_known_setting(key):
        try:
            coerced = settings_registry.coerce_setting_value(key, value)
        except Exception as exc:
            return abort(400, description=str(exc))
        settings_registry.save_settings({key: coerced})
        return ""
    config.storeValue(key, value)
    return ""


@app.route('/api/enso/settings')
@requires_auth
def get_enso_settings():
    payload = settings_registry.get_settings_payload()
    payload["raw_ensorc"] = settings_registry.read_ensorc_text()
    payload["config_dir"] = config.ENSO_USER_DIR.replace("\\", "/")
    payload["theme_choices"] = list(layout.COLOR_THEMES.keys())
    return json.dumps(payload), 200, {"Content-Type": "application/json"}


@app.route('/api/enso/settings/save', methods=["POST"])
@requires_auth
def post_enso_settings_save():
    payload = request.get_json(silent=True) or {}
    values = payload.get("values", {})
    raw_ensorc = payload.get("raw_ensorc")

    unknown = [key for key in values if not settings_registry.is_known_setting(key)]
    if unknown:
        return abort(400, description="Unknown setting(s): " + ", ".join(sorted(unknown)))

    coerced_values = {}
    for key, raw_value in values.items():
        try:
            coerced_values[key] = settings_registry.coerce_setting_value(key, raw_value)
        except Exception as exc:
            return abort(400, description=f"{key}: {exc}")

    settings_registry.save_settings(coerced_values, raw_text=raw_ensorc)
    return json.dumps({"status": "ok"}), 200, {"Content-Type": "application/json"}


@app.route('/api/enso/settings/reset', methods=["POST"])
@requires_auth
def post_enso_settings_reset():
    settings_registry.reset_to_defaults()
    payload = settings_registry.get_settings_payload()
    payload["raw_ensorc"] = settings_registry.read_ensorc_text()
    return json.dumps(payload), 200, {"Content-Type": "application/json"}


@app.route('/api/enso/get/config_dir')
@requires_auth
def get_enso_get_config_dir():
    return config.ENSO_USER_DIR.replace("\\", "/")


@app.route('/api/enso/open/config_dir')
@requires_auth
def get_enso_open_config_dir():
    os.startfile(config.ENSO_USER_DIR, "open")
    return ""


@app.route('/api/enso/get/ensorc')
@requires_auth
def get_enso_get_ensorc():
    return send_from_directory(config.ENSO_USER_DIR, "ensorc.py")


@app.route('/api/enso/set/ensorc', methods=["POST"])
@requires_auth
def post_enso_set_ensorc():
    with open(os.path.join(config.ENSO_USER_DIR, "ensorc.py"), "wb") as ensorc:
        ensorc.write(request.form["ensorc"].encode("utf-8"))
    return ""


@app.route('/api/enso/get/commands')
@requires_auth
def get_enso_get_commands():
    cmdman = CommandManager.get()
    commands = cmdman.getCommands()
    output = []

    for name, command in commands.items():
        desc = command.getDescription()
        helpText = command.getHelp()

        category = "other"
        if hasattr(command, "func") and hasattr(command.func, "category"):
            category = command.func.category

        file = ""
        if hasattr(command, "func") and hasattr(command.func, "cmdFile"):
            file = command.func.cmdFile

        cmdJSON = {"name": name, "description": desc, "help": helpText,
                   "category": category, "file": file}

        if name in config.DISABLED_COMMANDS:
            cmdJSON["disabled"] = "true"

        output = output + [cmdJSON]
    return json.dumps(output)


@app.route('/api/enso/get/user_command_categories')
@requires_auth
def get_enso_commands_categories():
    commands_dir = os.path.join(config.ENSO_USER_DIR, "commands")
    categories = []
    for f in os.listdir(commands_dir):
        if f.endswith(".py"):
            categories = categories + [os.path.splitext(f)[0]]
    return json.dumps(categories)


@app.route('/api/enso/commands/delete_category/<value>')
@requires_auth
def get_enso_commands_create_category(value):
    category_file = os.path.join(config.ENSO_USER_DIR, "commands", value + ".py")
    if os.path.exists(category_file):
        os.remove(category_file)
        ScriptTracker.get().setPendingChanges(category_file)
    return ""


@app.route('/api/enso/commands/write_category/<value>', methods=["POST"])
@requires_auth
def post_enso_commands_write_category(value):
    category_file = os.path.join(config.ENSO_USER_DIR, "commands", value + ".py")

    with open(category_file, "wb") as cat:
        cat.write(request.form["code"].encode("utf-8"))

    ScriptTracker.get().setPendingChanges(category_file)

    return ""


@app.route('/api/enso/commands/read_category/<value>')
@requires_auth
def get_enso_commands_read_category(value):
    return send_from_directory(os.path.join(config.ENSO_USER_DIR, "commands"), value + ".py")


@app.route('/api/enso/commands/disable/<path:command>')
@requires_auth
def get_enso_commands_disable(command):
    if command not in config.DISABLED_COMMANDS:
        config.DISABLED_COMMANDS += [command]
        config.COMMAND_STATE_CHANGED = True
        config.storeValue("DISABLED_COMMANDS", config.DISABLED_COMMANDS)
    return ""


@app.route('/api/enso/commands/enable/<path:command>')
@requires_auth
def get_enso_commands_enable(command):
    if command in config.DISABLED_COMMANDS:
        config.DISABLED_COMMANDS.remove(command)
        config.COMMAND_STATE_CHANGED = True
        config.storeValue("DISABLED_COMMANDS", config.DISABLED_COMMANDS)
    return ""


@app.route('/api/enso/write_tasks', methods=["POST"])
@requires_auth
def post_enso_commands_write_tasks():
    category_file = os.path.join(config.ENSO_USER_DIR, "tasks.py")

    with open(category_file, "wb") as tasks:
        tasks.write(request.form["code"].encode("utf-8"))
    return ""


@app.route('/api/enso/read_tasks')
@requires_auth
def get_enso_commands_read_tasks():
    return send_from_directory(config.ENSO_USER_DIR, "tasks.py")


class Httpd(threading.Thread):

    def __init__(self, app):
        threading.Thread.__init__(self)
        server_host = getattr(config, "WEBUI_HOST", None) or HOST
        self.srv = make_server(server_host, PORT, app, True)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.srv.serve_forever()

    def shutdown(self):
        self.srv.shutdown()


def displayMessage(msg):
    enso.messages.displayMessage("<p>%s</p>" % msg)


httpd = None


def start():
    global httpd
    httpd = Httpd(app)
    httpd.daemon = True
    httpd.start()


def stop():
    global httpd
    httpd.shutdown()
