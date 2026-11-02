"""指纹层：设备/UA/头部顺序。

微信风控第一层判的是「你是不是一个像人的客户端」：
- UA 里有没有 MicroMessenger、版本号是否连续；
- 头部集合与顺序是否稳定（同一身份内必须一致，跨身份才轮换）；
- Accept-Language / NetType / 屏幕参数是否自洽。

这里给出的是「身份（device）」而不是「随机 UA 串」——
一个 device 会同时产出 UA、屏幕、NetType、头部顺序，避免自相矛盾的组合。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, Iterable

# --------------------------------------------------------------------------- #
# 设备池：真机常见组合，字段自洽（机型/系统/微信版本/网络）
# --------------------------------------------------------------------------- #
ANDROID_DEVICES = [
    {"model": "SM-S9010", "brand": "samsung", "os": "Android 13", "build": "TP1A.220624.014",
     "wx": "8.0.49.2600(0x28003135)", "chrome": "116.0.0.0"},
    {"model": "M2012K11AC", "brand": "Xiaomi", "os": "Android 12", "build": "SKQ1.211006.001",
     "wx": "8.0.47.2560(0x28002F35)", "chrome": "107.0.0.0"},
    {"model": "PGBM10", "brand": "OPPO", "os": "Android 13", "build": "TP1A.220905.001",
     "wx": "8.0.50.2600(0x28003235)", "chrome": "118.0.0.0"},
    {"model": "V2118A", "brand": "vivo", "os": "Android 12", "build": "SP1A.210812.003",
     "wx": "8.0.45.2540(0x28002D35)", "chrome": "104.0.0.0"},
]

IOS_DEVICES = [
    {"model": "iPhone15,3", "os": "17.5.1", "wx": "8.0.50(0x18003228)"},
    {"model": "iPhone14,2", "os": "16.6", "wx": "8.0.47(0x18002F28)"},
    {"model": "iPhone13,4", "os": "17.2.1", "wx": "8.0.49(0x18003128)"},
    {"model": "iPhone12,1", "os": "16.7.2", "wx": "8.0.44(0x18002C28)"},
]

DESKTOP_UAS = [
    # 桌面浏览器（抓正文页用；工具类 UA 会被直接 302 到「环境异常」）
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
]

NET_TYPES = ("WIFI", "4G", "5G")


@dataclass(frozen=True)
class Device:
    """一个自洽的客户端身份。轮换粒度 = 指纹层最小单位。"""

    kind: str          # android / ios / desktop
    ua: str
    lang: str
    net_type: str
    screen: str
    dpr: str
    extra: Dict[str, str] = field(default_factory=dict, compare=False)

    def headers(
        self,
        *,
        referer: str = "https://mp.weixin.qq.com/",
        cookie: str = "",
        xhr: bool = False,
        origin: str = "",
    ) -> Dict[str, str]:
        """按 WebView/XHR 的真实顺序产头部。

        dict 保序，httpx 会按插入顺序发送——顺序本身是风控特征之一，
        不要在调用侧再塞新 key，需要额外头就用 extra 参数走 merge。
        """
        h: Dict[str, str] = {}
        if xhr:
            h["Accept"] = "application/json, text/javascript, */*; q=0.01"
            h["X-Requested-With"] = "XMLHttpRequest"
        else:
            h["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            )
        h["Accept-Encoding"] = "gzip, deflate, br"
        h["Accept-Language"] = self.lang
        h["Cache-Control"] = "no-cache"
        h["Pragma"] = "no-cache"
        if referer:
            h["Referer"] = referer
        if origin:
            h["Origin"] = origin
        h["User-Agent"] = self.ua
        h["Sec-Fetch-Dest"] = "empty" if xhr else "document"
        h["Sec-Fetch-Mode"] = "cors" if xhr else "navigate"
        h["Sec-Fetch-Site"] = "same-origin"
        h["Upgrade-Insecure-Requests"] = "1"
        if cookie:
            h["Cookie"] = cookie
        if self.kind != "desktop":
            # 微信 WebView 才会带的私有头，桌面浏览器不该有
            h["X-WECHAT-KEY"] = self.extra.get("wx_key", "")
            h["X-WECHAT-UIN"] = self.extra.get("wx_uin", "")
        return {k: v for k, v in h.items() if v != ""}


def _rand_hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def make_device(kind: str | None = None, rng: random.Random | None = None) -> Device:
    """产出一个自洽设备身份。kind 省略时按 6:3:1 抽 android/ios/desktop。"""
    rng = rng or random
    if kind is None:
        kind = rng.choices(("android", "ios", "desktop"), weights=(6, 3, 1))[0]
    net = rng.choice(NET_TYPES)

    if kind == "android":
        d = rng.choice(ANDROID_DEVICES)
        ua = (
            f"Mozilla/5.0 (Linux; {d['os']}; {d['model']} Build/{d['build']}; wv) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/{d['chrome']} "
            f"Mobile Safari/537.36 MicroMessenger/{d['wx']} WeChat/arm64 Weixin "
            f"NetType/{net} Language/zh_CN ABI/arm64"
        )
        return Device(
            kind="android",
            ua=ua,
            lang="zh-CN,zh;q=0.9,en;q=0.8",
            net_type=net,
            screen=f"{rng.choice((1080, 1170, 1260))}x{rng.choice((2340, 2400, 2532))}",
            dpr=rng.choice(("2.75", "3.0", "2.5")),
        )

    if kind == "ios":
        d = rng.choice(IOS_DEVICES)
        ua = (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {d['os'].replace('.', '_')} like Mac OS X) "
            f"AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            f"MicroMessenger/{d['wx']} NetType/{net} Language/zh_CN"
        )
        return Device(
            kind="ios",
            ua=ua,
            lang="zh-CN,zh-Hans;q=0.9",
            net_type=net,
            screen="1170x2532",
            dpr=rng.choice(("3", "2.5")),
        )

    ua = rng.choice(DESKTOP_UAS)
    return Device(
        kind="desktop",
        ua=ua,
        lang="zh-CN,zh;q=0.9,en;q=0.8",
        net_type=net,
        screen="1920x1080",
        dpr="1",
    )


def cookie_string(pairs: Iterable[tuple[str, str]] | dict) -> str:
    items = pairs.items() if isinstance(pairs, dict) else pairs
    return "; ".join(f"{k}={v}" for k, v in items if v)
