#!/usr/bin/env python3
"""
e-license 空位监控 · 轻量版（不开浏览器，只发网络请求）

每次运行 = 登录 -> 读取格子页 -> 找「予約可能」(绿色) 的格子 -> 有新空位就发邮件 -> 退出登录。
只通知，不自动预约。

用法:
  pip install requests beautifulsoup4
  python slot_watcher_lite.py              检查一次（GitHub Actions 就是这样定时调用的）
  python slot_watcher_lite.py --loop       一直循环检查（默认约每 3 分钟一次，在自己电脑上试用）
  RUN_MINUTES=10 python slot_watcher_lite.py   这次运行持续检查约 10 分钟后退出（GitHub Actions 用）
  python slot_watcher_lite.py --testmail   只发一封测试邮件
  python slot_watcher_lite.py --parse 某个页面.html   离线解析一个保存下来的格子页，用来排查

账号、密码、邮箱都从环境变量读取（代码里不放密码）:
  EL_ID, EL_PW, MAIL_USER, MAIL_PASS, MAIL_TO
本地运行时缺哪个就会提示你输入。
"""
import json
import os
import random
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DEFAULT_LOGIN_URL = "https://www.e-license.jp/el26/?abc=NVWLSxxtyBY%2BbrGQYS%2B1OA%3D%3D&senisakiCd=4"

STATE_FILE = "state.json"        # 记录已通知过的空位，避免重复发邮件
OUT_DIR = "output"               # 出错时保存页面，方便排查（只在本地用，别上传到公开的地方）
DEFAULT_INTERVAL = 180           # 两次检查的间隔（秒），可用环境变量 INTERVAL_SECONDS 覆盖
DEFAULT_MAX_BOOKINGS = 0         # 已预约（黄色）格子达到这个数量就自动停止监控；0=不自动停（默认）。环境变量 MAX_BOOKINGS 可改
STOP_FLAG = "stop.flag"          # 达到上限时写出这个文件，通知 GitHub Actions 关闭定时任务
STOP = "stop"
# 环境变量 CAR_KEYWORD：页面上的「車種」必须包含这个词（全角半角、大小写不分），否则报错停止通知。
# 例如 "AT1" 或 "所内"；留空 = 不检查车种。
TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CFG = {}


class TransientError(Exception):
    """网站临时问题（维护、访问集中、超时）。下次再试即可。"""


class FatalError(Exception):
    """需要人来处理：登录失败、页面结构变了等。"""


def log(msg):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config(need_login=True, need_mail=False):
    def get(name, prompt, required):
        v = os.environ.get(name, "").strip()
        if not v and required and sys.stdin.isatty():
            v = input(prompt).strip()
        if required and not v:
            raise FatalError(f"缺少配置 {name}")
        return v

    CFG["login_url"] = os.environ.get("EL_LOGIN_URL", "").strip() or DEFAULT_LOGIN_URL
    CFG["id"] = get("EL_ID", "教习生番号: ", need_login)
    CFG["pw"] = get("EL_PW", "密码: ", need_login)
    CFG["mail_user"] = get("MAIL_USER", "发件邮箱: ", need_mail)
    CFG["mail_pass"] = get("MAIL_PASS", "邮箱应用专用密码: ", need_mail)
    CFG["mail_to"] = os.environ.get("MAIL_TO", "").strip() or CFG["mail_user"]


# ---------------------------------------------------------------- 邮件
def send_mail(subject, body):
    if not (CFG.get("mail_user") and CFG.get("mail_pass")):
        log("邮件未配置（MAIL_USER / MAIL_PASS 为空），只在日志里提示：" + subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = CFG["mail_user"]
    msg["To"] = CFG["mail_to"]
    msg.set_content(body)

    pw = re.sub(r"\s+", "", CFG["mail_pass"])
    last_err = None
    for port, mode in ((465, "SSL"), (587, "STARTTLS")):
        try:
            if mode == "SSL":
                with smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30) as s:
                    s.login(CFG["mail_user"], pw)
                    s.send_message(msg)
            else:
                with smtplib.SMTP("smtp.gmail.com", port, timeout=30) as s:
                    s.starttls()
                    s.login(CFG["mail_user"], pw)
                    s.send_message(msg)
            log(f"邮件已发送（端口 {port}）")
            return True
        except Exception as e:
            last_err = e
            log(f"端口 {port} 发送失败：{type(e).__name__}")
    raise last_err


# ---------------------------------------------------------------- 网站交互
def dump(name, text):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        log(f"已保存页面到 {path}（本地排查用，里面有你的个人信息，别公开）")
    except Exception:
        pass


def check_site_error(text):
    if "EC05" in text or "システムメンテナンス" in text or "アクセス集中" in text:
        raise TransientError("网站返回了维护/访问集中提示（EC05）")


def http(s, method, url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    try:
        r = s.request(method, url, **kw)
    except requests.RequestException as e:
        raise TransientError(f"网络请求失败：{type(e).__name__}")
    r.encoding = "utf-8"
    if r.status_code >= 500:
        raise TransientError(f"网站返回 HTTP {r.status_code}")
    check_site_error(r.text)
    return r


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ja,zh-CN;q=0.8,en;q=0.5"})
    return s


def login(s):
    r = http(s, "GET", CFG["login_url"])
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.select_one("form#p01AForm") or soup.find("form")
    if form is None:
        dump("login_page.html", r.text)
        raise FatalError("登录页里找不到登录表单，页面结构可能变了")
    action = urljoin(r.url, form.get("action") or r.url)
    data = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
    data["studentId"] = CFG["id"]
    data["password"] = CFG["pw"]
    r2 = http(s, "POST", action, data=data, headers={"Referer": r.url})
    return r2


def get_grid_html(s, r):
    """登录后的页面如果已经是格子页就直接用；否则尝试点「技能予約」。"""
    if "yoyakuTable" in r.text:
        return r.text
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.select_one("form#p01AForm") or soup.select_one("input#password"):
        dump("login_failed.html", r.text)
        raise FatalError("登录失败：还停留在登录页，请检查教习生番号和密码")
    link = soup.select_one('a.dropdown-item[data-action*="/p03/p03a/nav"][data-kamoku="0"]')
    if link is not None:
        url = urljoin(r.url, link["data-action"])
        r3 = http(s, "POST", url, data={
            "schoolCd": link.get("data-schoolcd", ""),
            "kamokuCd": link.get("data-kamoku", "0"),
        }, headers={"Referer": r.url})
        if "yoyakuTable" in r3.text:
            return r3.text
        dump("after_nav.html", r3.text)
    else:
        dump("after_login.html", r.text)
    raise FatalError("登录后没找到格子页（yoyakuTable），请把 output 里的 html 文件发给我")


def logout(s, html):
    """尽量退出登录，释放会话（系统不允许同时多处登录）。"""
    try:
        a = BeautifulSoup(html, "html.parser").select_one('a[href*="/logout/"]')
        if a is not None:
            s.get(urljoin(CFG["login_url"], a["href"]), timeout=15)
    except Exception:
        pass


# ---------------------------------------------------------------- 解析格子
def parse_grid(html):
    """
    页面结构：左边 #ginou-scroll-day 放日期；右边 table.yoyakuTable 放格子，
    行 = 日期（tr.date），列 = 时限（表头 tr.carender）。
    格子状态类名：status0 空き無し / status1 予約可能(绿) / status3 予約済み(黄) / status9 灰色
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.yoyakuTable")
    if table is None:
        return None

    def clean(t):
        return re.sub(r"\s+", " ", t.replace("\xa0", " ")).strip()

    dates = [clean(td.get_text(" ")) for td in soup.select("#ginou-scroll-day tr.date td.view")]
    heads = []
    for td in table.select("tr.carender td"):
        t = clean(td.get_text(" "))
        m = re.match(r"(\d+)\s+(\d{1,2}:\d{2})", t)
        heads.append(f"第{m.group(1)}限 {m.group(2)}" if m else t)
    car_el = soup.select_one("div.carChange")
    car = re.sub(r"^車種\s*[:：]\s*", "", clean(car_el.get_text(" "))) if car_el else ""

    res = {"total": 0, "yellow": 0, "yellow_list": [], "green": [], "car": car}
    for i, tr in enumerate(table.select("tr.date")):
        for j, td in enumerate(tr.find_all("td", recursive=False)):
            res["total"] += 1
            cls = td.get("class") or []
            label = (f"{dates[i] if i < len(dates) else '第%d行' % (i + 1)}  "
                     f"{heads[j] if j < len(heads) else '第%d列' % (j + 1)}")
            if "status1" in cls:
                res["green"].append(label)
            elif "status3" in cls:
                res["yellow"] += 1
                name = clean(td.get_text(" "))
                res["yellow_list"].append(label + (f"（{name}）" if name else ""))
    return res


# ---------------------------------------------------------------- 主流程
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log(f"保存状态失败：{e}")


def write_stop_flag(msg):
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass


def check_once():
    s = new_session()
    html = ""
    try:
        r = login(s)
        html = get_grid_html(s, r)
        res = parse_grid(html)
        if res is None or res["total"] == 0:
            dump("no_cells.html", html)
            raise FatalError("读到的格子数为 0，页面结构可能变了")
    finally:
        logout(s, html)

    def norm(t):   # 全角转半角、转大写、去空格：「ＡＴ１」->「AT1」
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", t)).upper()

    kw = os.environ.get("CAR_KEYWORD", "").strip()
    if kw and res["car"] and norm(kw) not in norm(res["car"]):
        raise FatalError(f"当前车种是「{res['car']}」，不含「{kw}」。"
                         "如果你现在要约的就是这个车种，把 watch.yml 里的 CAR_KEYWORD 改成对应的词（或留空不检查）")

    state = load_state()
    if state.get("car") != res["car"]:      # 车种变了（例如从所内换成 AT1），旧的记录作废
        if state.get("car") is not None:
            log(f"车种变化：{state.get('car')} -> {res['car']}，重置已通知记录")
        state = {"car": res["car"]}
    seen = set(state.get("seen", []))
    current = set(res["green"])
    new = current - seen
    log(f"读到格子 {res['total']} 个，黄色 {res['yellow']} 个，"
        f"绿色 {len(current)} 个，新出现 {len(new)} 个")

    def lines(items):
        return "\n".join(f"  ・{x}" for x in items) if items else "  （无）"

    booked = f"你目前已预约的课（黄色，共 {res['yellow']} 个）：\n{lines(res['yellow_list'])}"

    # 已预约（黄色）的课达到上限 -> 通知一次并停止监控
    limit = int(os.environ.get("MAX_BOOKINGS", "") or DEFAULT_MAX_BOOKINGS)
    if limit > 0 and res["yellow"] >= limit:
        log(f"已有 {res['yellow']} 个黄色格子（已预约），达到上限 {limit}，停止监控")
        if not state.get("stop_notified"):
            sent = send_mail(
                f"【监控已停止】已预约满 {res['yellow']} 节课",
                f"已预约的课达到上限 {limit}，监控已自动停止。\n\n{booked}\n\n"
                "如果之后取消了课、想继续监控：在 GitHub 仓库的 Actions → slot-watch 里点 Enable workflow。",
            )
            if sent:
                state["stop_notified"] = True
        state["seen"] = sorted(current)
        save_state(state)
        return STOP
    state["stop_notified"] = False       # 低于上限了，复位

    if new:
        new_list = [g for g in res["green"] if g in new]          # 保持页面上的先后顺序
        others = [g for g in res["green"] if g not in new]        # 之前已通知过、仍然可约的
        body = (f"出现了 {len(new_list)} 个新的可预约（绿色）格子（车种：{res['car'] or '?'}）：\n"
                f"{lines(new_list)}\n")
        if others:
            body += f"\n另外仍可预约的（之前已通知过）：\n{lines(others)}\n"
        body += f"\n{booked}\n\n马上去抢：{CFG['login_url']}"
        subject = "【有空位】" + new_list[0] + (f" 等 {len(new_list)} 个" if len(new_list) > 1 else "")
        if not send_mail(subject, body):   # 没发出去就不记为已通知，下次继续提醒
            return None
    state["seen"] = sorted(current)   # 已经消失的空位会被移出，下次再出现会重新通知
    save_state(state)
    return None


def run_loop(interval, deadline=None, max_fails=5):
    """循环检查。deadline 为 None 表示一直跑；否则到点后退出。连续失败 max_fails 次返回 1。"""
    fails = 0
    while True:
        try:
            if check_once() == STOP:
                write_stop_flag("booking limit reached")
                return 0
            fails = 0
        except TransientError as e:
            log(f"暂时失败，稍后重试：{e}")
        except FatalError as e:
            fails += 1
            log(f"出错（{fails}/{max_fails}）：{e}")
        except Exception as e:
            fails += 1
            log(f"未预料的错误（{fails}/{max_fails}）：{type(e).__name__}: {e}")
        if fails >= max_fails:
            return 1
        wait = random.uniform(interval * 0.85, interval * 1.15)
        if deadline is not None and time.time() + wait >= deadline:
            return 0
        time.sleep(wait)


def main():
    argv = sys.argv[1:]

    if argv[:1] == ["--parse"]:
        with open(argv[1], encoding="utf-8") as f:
            print(json.dumps(parse_grid(f.read()), ensure_ascii=False, indent=2))
        return 0

    if "--testmail" in argv:
        load_config(need_login=False, need_mail=True)
        send_mail("【测试】预约监控邮件", "能收到这封邮件，说明邮件配置正确。")
        return 0

    try:
        load_config(need_login=True, need_mail=False)
    except FatalError as e:
        log(str(e))
        return 1

    if os.path.exists(STOP_FLAG):
        os.remove(STOP_FLAG)

    interval = float(os.environ.get("INTERVAL_SECONDS", "") or DEFAULT_INTERVAL)
    run_minutes = float(os.environ.get("RUN_MINUTES", "") or 0)

    if "--loop" in argv:
        log("循环模式，Ctrl+C 停止")
        return run_loop(interval, None, 5)

    if run_minutes > 0:
        log(f"本次运行持续约 {run_minutes:g} 分钟，每约 {interval:g} 秒检查一次")
        return run_loop(interval, time.time() + run_minutes * 60, 3)

    try:
        if check_once() == STOP:
            write_stop_flag("booking limit reached")
        return 0
    except TransientError as e:
        log(f"暂时失败，下次再试：{e}")
        return 0
    except FatalError as e:
        log(f"出错：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
