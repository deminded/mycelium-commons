# Служба проверки вложений: установка (#2559, часть 2)

**Зачем.** Разбор вложения (libmagic, zipfile/XML, libclamav) идёт на файлах от посторонних.
Под учёткой агента эксплойт в разборщике получил бы её дом: токены, ключи, сессию Telegram.
Служба уносит разбор в экземпляр systemd с динамическим uid: без `/home`, без сети,
с частным `/tmp`, не дольше 160 с. Агент отдаёт байты через сокет, а не путь.

Код (`/opt/file-safety`) принадлежит учётке агента: правка проверки обходится без root,
а службе каталог доступен только на чтение (`ProtectSystem=strict`). Юниты ставит root.

## Команды root — один заход

```sh
sudo install -d -o claude-user -g claude-user -m 755 /opt/file-safety
D=/home/claude-user/mycelium-commons/means/file-safety/deploy
sha256sum "$D/file-safety.socket" "$D/file-safety@.service"   # сверить с присланными суммами
sudo install -m 644 -o root -g root "$D/file-safety.socket" "$D/file-safety@.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now file-safety.socket
ls -l /run/file-safety.sock          # ожидается srw------- claude-user claude-user
```

Пока агент не переключён (`TRACT_FILE_SAFETY_MODE=socket`), проверка идёт по-старому:
включение сокета само ничего не меняет.

## Откат

```sh
sudo systemctl disable --now file-safety.socket
sudo rm /etc/systemd/system/file-safety.socket /etc/systemd/system/file-safety@.service
sudo systemctl daemon-reload
```

## Дальше — без root (агент)

1. Код в `/opt/file-safety`: `file_safety_check.py`, `file_safety_service.py`, `file_safety_client.py`
   (из `../code/`).
2. Проба изоляции — мерить песочницу, а не юнит-файл:
   `python3 /opt/file-safety/file_safety_client.py probe <свой дом> <каталог секретов> <каталог медиа>`
   — rc 0 только если все чтения, запись в каталог кода и сетевое соединение отказаны
   и uid экземпляра не равен uid агента.
3. Живые образцы через `check`: EICAR → reject; текст, docx, ogg, mp4 → те же вердикты,
   что у нынешнего гейта.
4. Переключить агента: `TRACT_FILE_SAFETY_MODE=socket` в окружении тракта, перезапуск тракта.

## Пределы

- `MaxConnections=1`: если отменённая проверка ещё доживает свой срок, следующую служба
  отвергнет — файл уйдёт в карантин «без ответа», а не пройдёт.
- `MemoryDenyWriteExecute` не включён: без стенда не проверено, переживёт ли его libclamav.
- Динамический uid читает то, что открыто всем вне `/home` и `/tmp` (`/etc`, `/opt`, `/var`).
  Сверено 13.11.2026 поиском по именам (`*.env*`, `*secret*`, `*token*`, `*.session`, `*.key`, `*.pem`)
  в `/opt /etc /var/lib /srv /usr/local`: всем читаемы `msconsult.env` (id чата и путь к токену, не токен),
  публичный `proxy-secret` MTProxy, шаблон fail2ban без токена, ключ fwupd. Секретов агента нет.
  Предел: искал по именам, не по содержимому.
