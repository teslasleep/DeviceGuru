#!/usr/bin/env python3
"""
Обновляет список устройств для генератора DeviceGuru.

Что делает:
  1. Скачивает https://api.ipsw.me/v4/devices и сохраняет как Devices.json
  2. Конвертирует Devices.json в GeneratorDeviceList.plist:
       - уже существующие записи (name/enum) сохраняются как есть,
       - для новых идентификаторов генерируется enum по названию устройства,
       - устройства, которых нет в ipsw.me (Watch, симуляторы и т.д.), не удаляются,
       - неподдерживаемые генератором семейства (Mac, iBridge, ...) пропускаются.
  3. (опционально) запускает `swift main.swift`

Использование (из папки, где лежит main.swift):
  ./update_devices.py                  # скачать + обновить plist
  ./update_devices.py --run-generator  # ... и запустить main.swift
  ./update_devices.py --no-download    # не скачивать, взять текущий Devices.json
  ./update_devices.py --refresh-names  # обновить name у существующих записей из JSON
  ./update_devices.py --dry-run        # только показать, что изменится

Зависимости: только стандартная библиотека Python 3.8+.
"""

import argparse
import json
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.ipsw.me/v4/devices"
BASE_DIR = Path(__file__).resolve().parent
JSON_PATH = BASE_DIR / "Devices.json"
PLIST_PATH = BASE_DIR / "GeneratorDeviceList.plist"

# Должно совпадать с префиксами, которые обрабатывает main.swift
SUPPORTED_PREFIXES = ("iPhone", "iPod", "iPad", "Watch", "AppleTV")
SIMULATOR_IDS = ("i386", "x86_64", "arm64")


# --------------------------------------------------------------------------- download

def download_json(url: str, retries: int = 3, timeout: int = 30) -> list:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "DeviceGuru-generator", "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, list):
                raise ValueError("ответ API - не массив")
            return data
        except (urllib.error.URLError, ValueError, TimeoutError) as error:
            last_error = error
            print(f"  попытка {attempt}/{retries} не удалась: {error}")
            if attempt < retries:
                time.sleep(2 * attempt)
    sys.exit(f"Не удалось скачать {url}: {last_error}")


def validate_devices(devices: list, previous_count: int) -> None:
    if not devices:
        sys.exit("API вернул пустой список - файлы не изменены.")
    for device in devices:
        if not isinstance(device, dict) or "identifier" not in device or "name" not in device:
            sys.exit(f"Неожиданный формат записи: {device!r} - файлы не изменены.")
    if previous_count and len(devices) < previous_count * 0.8:
        sys.exit(
            f"В новом списке {len(devices)} устройств против {previous_count} в старом - "
            "похоже на неполный ответ API. Файлы не изменены."
        )


def write_json(path: Path, devices: list) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(devices, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- enum generation

CHIP_RE = re.compile(r"^(M\d+|A\d+)$", re.I)


def make_enum(name: str) -> str:
    """
    Строит имя enum по названию устройства. Примеры:
      'iPad Air 11-inch (M3, WiFi)'            -> IPAD_AIR_11_M3
      'iPad Air 11-inch (M3, Cellular)'        -> IPAD_AIR_11_M3_CELLULAR
      'iPad (A16, WiFi)'                       -> IPAD_A16
      'iPad (WiFi, 10th generation)'           -> IPAD_10G_WIFI
      'iPhone 17e'                             -> IPHONE_17_E
      'Apple Watch Series 11 (46mm, LTE)'      -> APPLE_WATCH_SERIES_11_46_CELLULAR
    """
    text = name.strip()
    main, _, rest = text.partition("(")
    groups = [g for g in re.split(r"[,/+]", rest.replace(")", " ")) if g.strip()] if rest else []
    # скобок может быть несколько: "(11-inch, WiFi) (3rd generation)"
    groups = [g.replace("(", " ") for g in groups]

    def tokens(s: str) -> list:
        s = s.replace("Wi-Fi", "WiFi").replace("-inch", "").replace(" inch", "")
        s = re.sub(r"(\d)\s*mm\b", r"\1", s)
        s = re.sub(r"(\d+(?:st|nd|rd|th))\s+generation", lambda m: re.sub(r"\D", "", m.group(1)) + "G", s, flags=re.I)
        s = re.sub(r"(\d)(e)\b", r"\1 \2", s) if s.strip().lower().startswith("iphone") else s
        s = re.sub(r"[^0-9A-Za-z]+", " ", s)
        return [t.upper() for t in s.split()]

    base = tokens(main)
    size, generation, chip, connectivity, extra = [], [], [], [], []
    for group in groups:
        for t in tokens(group):
            if re.fullmatch(r"\d+", t):
                size.append(t)  # размер экрана / корпуса: 11, 13, 46
            elif re.fullmatch(r"\d+G", t):
                generation.append(t)
            elif CHIP_RE.match(t):
                chip.append(t)
            elif t in ("WIFI", "CELLULAR", "LTE"):
                connectivity.append("CELLULAR" if t == "LTE" else t)
            elif t == "CHINA":
                extra.append("CN")
            else:
                extra.append(t)

    # у устройств с чипом (M/A-серия) Wi-Fi - вариант по умолчанию и в имени не указывается
    if chip:
        connectivity = [c for c in connectivity if c != "WIFI"]
    seen, conn = set(), []
    for c in connectivity:
        if c not in seen:
            seen.add(c)
            conn.append(c)

    parts = base + size + generation + chip + extra + conn
    # "iPad 11-inch" + M-чип: дублей быть не должно
    result, last = [], None
    for p in parts:
        if p != last:
            result.append(p)
        last = p
    return "_".join(result)


# --------------------------------------------------------------------------- plist conversion

def group_order(identifier: str) -> int:
    if identifier in SIMULATOR_IDS:
        return len(SUPPORTED_PREFIXES)
    for index, prefix in enumerate(SUPPORTED_PREFIXES):
        if identifier.startswith(prefix):
            return index
    return len(SUPPORTED_PREFIXES) + 1


def version_key(identifier: str):
    match = re.search(r"(\d+),(\d+)", identifier)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def sort_key(identifier: str):
    # "iPad1,1" должен идти до "iPad10,1" - сортируем по числам, а не по строке
    return (group_order(identifier), version_key(identifier), identifier)


def convert(devices: list, existing: dict, refresh_names: bool):
    result = {k: dict(v) for k, v in existing.items()}
    used_enums = {v["enum"] for v in result.values() if "enum" in v}
    added, renamed, skipped = [], [], {}

    for device in devices:
        identifier = device["identifier"]
        name = device["name"].strip()

        if not identifier.startswith(SUPPORTED_PREFIXES):
            family = re.sub(r"[\d,]+$", "", identifier)
            skipped[family] = skipped.get(family, 0) + 1
            continue

        if identifier in result:
            if refresh_names and result[identifier].get("name") != name:
                renamed.append((identifier, result[identifier].get("name"), name))
                result[identifier]["name"] = name
            continue

        enum = make_enum(name)
        if not enum:
            print(f"  ! не удалось построить enum для {identifier} ({name}) - пропущено")
            continue
        if enum in used_enums:
            # одинаковый enum у разных устройств молча подменил бы одно другим
            unique = f"{enum}_{re.sub(r'[^0-9]+', '_', identifier).strip('_')}"
            print(f"  ! enum {enum} уже занят, для {identifier} использован {unique} - проверьте вручную")
            enum = unique
        used_enums.add(enum)
        result[identifier] = {"name": name, "enum": enum}
        added.append(identifier)

    ordered = {k: {"name": result[k]["name"], "enum": result[k]["enum"]} for k in sorted(result, key=sort_key)}
    return ordered, added, renamed, skipped


# --------------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Обновление Devices.json и GeneratorDeviceList.plist")
    parser.add_argument("--no-download", action="store_true", help="не скачивать, использовать текущий Devices.json")
    parser.add_argument("--refresh-names", action="store_true", help="обновить name у существующих записей")
    parser.add_argument("--run-generator", action="store_true", help="после обновления запустить swift main.swift")
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать, только показать изменения")
    args = parser.parse_args()

    previous_count = 0
    if JSON_PATH.exists():
        try:
            previous_count = len(json.loads(JSON_PATH.read_text(encoding="utf-8")))
        except ValueError:
            pass

    if args.no_download:
        if not JSON_PATH.exists():
            sys.exit(f"{JSON_PATH.name} не найден")
        devices = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        print(f"Использую существующий {JSON_PATH.name}: {len(devices)} устройств")
    else:
        print(f"Скачиваю {API_URL}")
        devices = download_json(API_URL)
        validate_devices(devices, previous_count)
        print(f"Получено устройств: {len(devices)} (было {previous_count})")
        if not args.dry_run:
            write_json(JSON_PATH, devices)
            print(f"Сохранено: {JSON_PATH.name}")

    existing = {}
    if PLIST_PATH.exists():
        with PLIST_PATH.open("rb") as f:
            existing = plistlib.load(f)

    ordered, added, renamed, skipped = convert(devices, existing, args.refresh_names)

    print(f"\nДобавлено в plist: {len(added)}")
    for identifier in added:
        print(f"  + {identifier:<12} {ordered[identifier]['enum']:<40} {ordered[identifier]['name']}")
    if renamed:
        print(f"Переименовано: {len(renamed)}")
        for identifier, old, new in renamed:
            print(f"  ~ {identifier}: {old!r} -> {new!r}")
    if skipped:
        summary = ", ".join(f"{family} ({count})" for family, count in sorted(skipped.items()))
        print(f"Пропущено (генератор не поддерживает): {summary}")
    missing = sorted(set(existing) - {d["identifier"] for d in devices})
    if missing:
        print(f"Остались в plist, но отсутствуют в JSON ({len(missing)}): оставлены без изменений")

    if args.dry_run:
        print("\n--dry-run: файлы не изменены")
        return

    tmp = PLIST_PATH.with_suffix(".plist.tmp")
    with tmp.open("wb") as f:
        plistlib.dump(ordered, f, sort_keys=False)
    tmp.replace(PLIST_PATH)
    print(f"\nСохранено: {PLIST_PATH.name} ({len(ordered)} записей)")

    if args.run_generator:
        print("\nЗапускаю swift main.swift")
        subprocess.run(["swift", "main.swift"], cwd=BASE_DIR, check=True)


if __name__ == "__main__":
    main()
