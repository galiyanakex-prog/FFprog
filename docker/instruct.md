# Инструкция: как собрать образ FFprog

Эта инструкция — для тех, кто хочет собрать Docker-образ FFprog на своём компьютере.
Никаких знаний программирования не нужно. Всё делается по шагам.

---

## Что такое Docker-образ?

Представь, что FFprog — это приложение, которому для работы нужен Python и FFmpeg.
На разных компьютерах эти программы могут быть по-разному установлены, а могут и не быть.

**Docker-образ** — это «консервная банка», в которой уже есть всё: Python, FFmpeg,
шрифты, настройки. Ты берёшь банку, переносишь на другой компьютер, открываешь —
и программа работает. Как переносная версия, только надёжнее.

---

## Что нужно перед началом

1. **Компьютер на Linux** (amd64 — это обычный процессор Intel или AMD).
2. **Docker** установлен. Проверь:
   ```bash
   docker --version
   ```
   Если пишет версию — всё хорошо. Если нет — установи Docker.
3. **Доступ к Docker без sudo**. Если команда `docker info` говорит «permission denied» —
   читай раздел «Проблема: docker требует sudo» в конце этой инструкции.
4. **Интернет** — для первого раза нужен, чтобы скачать образ (около 200 МБ).
5. **Достаточно места** — образ займёт около 1 ГБ на диске.

---

## Шаг 1. Скачай проект FFprog

Если у тебя уже есть папка `FFprog` — пропускай.

```bash
# Если проект в архиве — распакуй его.
# Если скачал через git:
git clone <адрес_репозитория>
cd FFprog
```

В папке должны быть файлы:
```
Dockerfile          build_image.py    run-ffprog.sh       docker/
app.py              ffmpeg_engine.py  tests/
```

---

## Шаг 2. Собери образ

В папке `FFprog` выполни:

```bash
sudo python3 build_image.py
```

**Что произойдёт:**
1. Скрипт проверит, что Docker работает.
2. Скачает базовый образ Debian (около 200 МБ).
3. Установит внутрь Python, FFmpeg, шрифты и всё остальное.
4. Скопирует код FFprog внутрь.
5. Запустит проверку — создаст тестовое видео и проверит, что все инструменты работают.
6. Покажет размер образа и команды для запуска.

**Сколько времени:** 5–15 минут (зависит от скорости интернета и компьютера).

**Что видишь на экране:**
```
=== Сборка образа ffprog:latest ===
... (много текста с процентами) ...
Образ ffprog:latest собран.

=== Smoke-тест ===
[OK] ffmpeg: /usr/bin/ffmpeg
[OK] кодеки: все 9 нужных на месте
...
Итог: ошибок 0, предупреждений 2

=== Unit-тесты ===
Ran 45 tests in 9.186s
OK

=== Размер образа ===
  Размер: 938 МБ
```

Если видишь `OK` в конце — **всё прошло успешно**. Можно переходить к шагу 3.

---

## Шаг 3. Запусти программу

### 3.1 Запуск с окном (GUI)

```bash
sudo HOME=/home/u ./run-ffprog.sh
```

> **Важно:** замени `/home/u` на путь к своей домашней папке.
> Посмотри свой путь командой: `echo $HOME`

Если окно не открылось и видишь ошибку `Authorization required` или
`cannot open display` — выполни:

```bash
xhost +SI:localuser:root
sudo HOME=/home/u ./run-ffprog.sh
```

### 3.2 Проверка без окна

Если окна нет (например, на сервере), можно проверить, что всё работает:

```bash
# Показать версии программ внутри образа
sudo docker run --rm ffprog:latest version

# Запустить smoke-тест (проверка кодеков и движка)
sudo docker run --rm ffprog:latest smoke

# Запустить все тесты
sudo docker run --rm ffprog:latest test

# Открыть оболочку внутри образа
sudo docker run --rm -it ffprog:latest bash
```

---

## Шаг 4. Перенос на другой компьютер

### 4.1 Сохрани образ в файл

```bash
docker save ffprog:latest | gzip > ffprog.tar.gz
```

Файл `ffprog.tar.gz` — это и есть твой образ. Его можно скопировать на флешку,
отправить по почте, загрузить в облако.

### 4.2 Загрузи на другом компьютере

На новом компьютере (где установлен Docker):

```bash
docker load < ffprog.tar.gz
```

Готово! Образ установлен. Запускай как в шаге 3.

---

## Частые проблемы и решения

### Проблема: `docker: command not found`

**Что это:** Docker не установлен.

**Решение:**
```bash
# Debian / Ubuntu:
sudo apt update
sudo apt install docker.io
sudo systemctl start docker
sudo systemctl enable docker
```

### Проблема: `permission denied while trying to connect to the docker API`

**Что это:** Твой пользователь не имеет доступа к Docker.

**Решение (пошагово):**

```bash
# 1. Добавь себя в группу docker
sudo usermod -aG docker $USER

# 2. Перезайди в систему (выйди и войди снова)
#    ИЛИ выполни:
newgrp docker

# 3. Проверь
docker info --format 'OK: {{.ServerVersion}}'
```

Если после этого всё равно не работает — используй `sudo`:
```bash
sudo python3 build_image.py
```

### Проблема: `network is unreachable` при сборке

**Что это:** Docker пытается скачать образ по IPv6, но IPv6 не работает.

**Решение:**

```bash
# 1. Предпочитай IPv4
sudo cp /etc/gai.conf /etc/gai.conf.bak.ffprog
echo 'precedence ::ffff:0:0/96  10' | sudo tee -a /etc/gai.conf

# 2. Перезапусти Docker
sudo systemctl restart docker

# 3. Проверь
sudo docker pull debian:trixie-slim
```

Если `pull` прошёл — собирай образ заново:
```bash
sudo python3 build_image.py
```

**Как откатить:**
```bash
sudo mv /etc/gai.conf.bak.ffprog /etc/gai.conf
sudo systemctl restart docker
```

### Проблема: `Образ ffprog:latest не найден`

**Что это:** Образ ещё не собран.

**Решение:** Собери его (Шаг 2).

### Проблема: Окно не открывается, ошибка `cannot open display`

**Что это:** Контейнер не может подключиться к графическому серверу.

**Решение:**

```bash
# Разреши контейнеру подключиться к X-серверу
xhost +SI:localuser:root

# Запусти снова
sudo HOME=/home/u ./run-ffprog.sh
```

### Проблема: `sudo: python3: command not found`

**Что это:** Python 3 не установлен.

**Решение:**
```bash
sudo apt install python3
```

### Проблема: `tini: не найден` в smoke-тесте

**Что это:** Не критично. Tini — маленькая утилита для сбора «зомби-процессов».
В Debian trixie её нет в репозитории, но она не нужна для работы FFprog.

**Решение:** Ничего. Это предупреждение, не ошибка.

---

## Полезные команды

| Команда | Что делает |
|---|---|
| `sudo python3 build_image.py` | Собрать образ |
| `sudo python3 build_image.py --no-test` | Собрать без тестов (быстрее) |
| `sudo python3 build_image.py --tag my/ffprog:1.0` | Собрать с другим тегом |
| `sudo HOME=/home/u ./run-ffprog.sh` | Запустить GUI |
| `sudo docker run --rm ffprog:latest version` | Показать версии внутри образа |
| `sudo docker run --rm ffprog:latest smoke` | Проверить образ |
| `sudo docker run --rm ffprog:latest test` | Запустить тесты |
| `sudo docker run --rm -it ffprog:latest bash` | Открыть оболочку внутри образа |
| `docker save ffprog:latest \| gzip > ffprog.tar.gz` | Сохранить образ в файл |
| `docker load < ffprog.tar.gz` | Загрузить образ из файла |
| `docker images` | Показать все образы |
| `docker rmi ffprog:latest` | Удалить образ |
| `docker ps -a` | Показать все контейнеры |

---

## Что внутри образа?

| Компонент | Зачем нужен |
|---|---|
| **Debian trixie** | Базовая операционная система |
| **Python 3.13** | Язык, на котором написана FFprog |
| **Tkinter 8.6** | Графический интерфейс (окна, кнопки) |
| **FFmpeg 7.x** | Обработка видео и звука |
| **FFprobe** | Анализ файлов (длительность, кодеки) |
| **x11-utils, xauth** | Подключение к графическому серверу |
| **pcmanfm** | Файловый менеджер (кнопка «Открыть папку») |
| **dbus-x11** | Вспомогательная утилита для xdg-open |
| **fonts-dejavu, fonts-liberation** | Шрифты с поддержкой кириллицы |
| **ru_RU.UTF-8** | Русская локаль (чтобы буквы не превращались в кракозябры) |
| **shared-mime-info, file** | Определение типов файлов |

**Итого:** 938 МБ. Всё в одной банке.

---

## Как это работает (кратко)

```
Твой компьютер                    Docker-образ
┌──────────────┐                ┌──────────────────┐
│  FFprog      │                │  Python 3.13     │
│  (код)       │  ──копируется──▶│  Tkinter 8.6     │
│              │                │  FFmpeg 7.x      │
│  Docker      │  ──запускает──▶│  Шрифты          │
│  (дверь)     │                │  Локаль ru_RU    │
└──────────────┘                └──────────────────┘
```

1. `build_image.py` берёт код FFprog и «закатывает» его в образ.
2. `run-ffprog.sh` открывает дверь в образ и запускает программу.
3. Образ видит твои файлы через монтирование `$HOME`.
4. Окно программы рисуется на твоём экране через X11.

---

## Что делать после сборки

1. **Проверь, что всё работает:** `sudo HOME=/home/u ./run-ffprog.sh`
2. **Попробуй обработать видео:** выбери файл, выбери действие, нажми «Преобразовать».
3. **Перенеси на другой ПК:** `docker save ffprog:latest | gzip > ffprog.tar.gz`
4. **На другом ПК:** `docker load < ffprog.tar.gz` и запусти.

---

## Если что-то пошло не так

1. Прочитай раздел «Частые проблемы» выше.
2. Если не помогло — покажи вывод команды:
   ```bash
   docker info
   docker images
   sudo docker run --rm ffprog:latest smoke
   ```
3. Если образ сломался — удали и собери заново:
   ```bash
   sudo docker rmi ffprog:latest
   sudo python3 build_image.py
   ```

---

**Готово!** Теперь у тебя есть FFprog в банке, которую можно переносить куда угодно.
