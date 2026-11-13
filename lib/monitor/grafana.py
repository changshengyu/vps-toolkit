"""Local Grafana account and resource management; credentials arrive on stdin."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


ORIGIN = "http://127.0.0.1:3000"


class Error(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def save(path, value):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def account(directory):
    path = directory / "grafana-account.json"
    if not path.exists():
        return {"login": "admin", "initial_password_stale": False}
    value = json.loads(path.read_text())
    if value.get("managed_by") != "vps-toolkit-grafana-v1" or not valid_login(value.get("login", "")):
        raise Error("Grafana 账户记录无效。")
    return value


def valid_login(value):
    return re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", value) is not None


def validate(login, password):
    if not valid_login(login):
        raise Error("用户名限 1～64 个字母、数字或 _ . @ -。")
    if password and (len(password) < 12 or len(password.encode()) > 128 or any(ord(c) < 32 for c in password)):
        raise Error("新密码至少 12 个字符、最多 128 字节，不能含控制字符。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, login, password):
        self.login, self.password = login, password
        # The fixed loopback endpoint must never use a system HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, data=None):
        auth = base64.b64encode((self.login + ":" + self.password).encode()).decode()
        request = urllib.request.Request(ORIGIN + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"}, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise Error(f"Grafana API HTTP {exc.code}；401 请检查当前账号密码，403 请使用服务器管理员。", exc.code) from None
        except (OSError, ValueError):
            raise Error("Grafana 本机接口未就绪或返回异常，请先运行 grafana-check。") from None


def change(directory, login, password, new_login, new_password, api_factory=API):
    validate(new_login, new_password)
    if not password:
        # Only accept the exact installer-generated hex password, never source .env.
        lines = (directory / ".env").read_text().splitlines()
        values = [v.split("=", 1)[1] for v in lines if re.fullmatch(r"GRAFANA_ADMIN_PASSWORD=[a-f0-9]{48}", v)]
        if len(values) != 1:
            raise Error("无法读取安装时初始密码，请手动输入当前密码。")
        password = values[0]
    api = api_factory(login, password)
    current = api.call("GET", "/api/user")
    if not current.get("isGrafanaAdmin"):
        raise Error("当前账号不是 Grafana 服务器管理员。")
    user_id = int(current["id"])
    if current["login"] != new_login:
        try:
            existing = api.call("GET", "/api/users/lookup?" + urllib.parse.urlencode({"loginOrEmail": new_login}))
        except Error as exc:
            if exc.status != 404:
                raise
            existing = None
        if existing and int(existing["id"]) != user_id:
            raise Error("新用户名已被其他用户使用，未修改账户。")
        api.call("PUT", f"/api/users/{user_id}", {
            "login": new_login, "name": current.get("name", ""), "email": current.get("email", "")})
        print("用户名已更新为：" + new_login, flush=True)
    # Journal each successful step: a later password error must not hide a rename.
    previous = account(directory)
    record = {"managed_by": "vps-toolkit-grafana-v1", "login": new_login, "id": user_id,
              "initial_password_stale": previous.get("initial_password_stale", False)}
    save(directory / "grafana-account.json", record)
    api = api_factory(new_login, password)
    if new_password:
        try:
            api.call("PUT", "/api/user/password", {
                "oldPassword": password, "newPassword": new_password, "confirmNew": new_password})
        except Error:
            raise Error(f"用户名当前为 {new_login}；密码修改未确认成功，请先用原密码尝试登录，勿只修改 .env。") from None
        record["initial_password_stale"] = True
        save(directory / "grafana-account.json", record)
        api = api_factory(new_login, new_password)
    verified = api.call("GET", "/api/user")
    if int(verified["id"]) != user_id or verified["login"] != new_login:
        raise Error("账户已修改，但登录验证异常，请检查 Grafana 日志。")
    print("登录验证通过；用户名：" + new_login + "。请使用设置的密码登录。")


def resources(directory, memory):
    if memory not in (256, 320, 384, 512):
        raise Error("支持的内存上限：256、320、384、512 MiB。")
    path = directory / "compose.grafana.yaml"
    if path.exists() and json.loads(path.read_text()).get("x-vps-toolkit-grafana-resources") != 1:
        raise Error("发现非本工具管理的同名配置，未覆盖。")
    save(path, {"x-vps-toolkit-grafana-resources": 1, "services": {"grafana": {
        "mem_limit": f"{memory}m", "memswap_limit": f"{memory}m", "cpus": 0.5,
        "environment": {"GOMEMLIMIT": f"{memory // 2}MiB"}}}})


def main():
    action, directory = sys.argv[1], Path(sys.argv[2])
    try:
        if action == "account":
            value = account(directory)
            print(value["login"])
            if value.get("initial_password_stale"):
                print("密码已通过菜单修改；.env 中安装时的初始密码不再适用。")
            else:
                print("安装时初始密码在 .env；若已在网页改过，请使用当前密码。")
        elif action == "login":
            print(account(directory)["login"])
        elif action == "change":
            fields = sys.stdin.buffer.read(4096).decode().split("\0")
            if len(fields) != 5 or fields[-1] != "":
                raise Error("账户输入格式无效。")
            change(directory, *fields[:4])
        elif action == "resources":
            resources(directory, int(sys.argv[3]))
        else:
            raise Error("未知 Grafana 操作。")
    except (Error, ValueError, OSError, KeyError, TypeError):
        exc = sys.exc_info()[1]
        print("错误：" + (str(exc) if isinstance(exc, Error) else "Grafana 本地配置无效或接口返回异常。"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
