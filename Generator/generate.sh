#/bin/sh
#./update_devices.py                  # скачать, сохранить Devices.json, обновить plist
#./update_devices.py --run-generator  # то же + запустить swift main.swift
#./update_devices.py --dry-run        # показать, что изменится, ничего не записывая
#./update_devices.py --no-download    # не скачивать, взять текущий Devices.json
./update_devices.py --refresh-names  # заодно обновить name у существующих записей