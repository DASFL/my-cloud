# My Cloud

My Cloud — дипломный проект по курсу «Fullstack-разработчик на Python». Это небольшое облачное хранилище, где пользователь сможет загружать файлы, скачивать их и делиться ссылками.

Сейчас в проекте есть API регистрации, сессионного входа, загрузки и списка своих файлов, а также главная страница React с заглушкой «My Cloud — в разработке».

## Что планируется сделать

- Формы регистрации и входа в интерфейсе.
- Интерфейс загрузки и списка файлов, скачивание, удаление и переименование файлов.
- Комментарии к файлам и ссылки для скачивания без авторизации.
- Список пользователей и управление их файлами для администратора.

## Технологии

- Python, Django, Django REST Framework.
- PostgreSQL.
- JavaScript, React, Redux, React Router.
- Vite для сборки фронтенда.

## Локальная проверка и запуск

Для запуска нужны Python 3.12, PostgreSQL и Node.js 24 с npm. Серверную часть проверяли с PostgreSQL 17.

### PostgreSQL

На macOS можно установить [Postgres.app](https://postgresapp.com/documentation/install.html) в `Applications`, запустить приложение и выбрать `Initialize`. После запуска PostgreSQL один раз создайте роль и базу проекта:

```sh
export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"
pg_isready -h 127.0.0.1 -p 5432
createuser --pwprompt my_cloud
createdb --owner=my_cloud my_cloud
```

Если в этой рабочей папке уже настроен локальный экземпляр в `pgdata/`, после остановки запустите его из корня проекта:

```sh
pgdata/Postgres.app/Contents/Versions/17/bin/pg_ctl -D pgdata/data -l pgdata/postgres.log \
  -o "-h localhost -p 5432 -k '$PWD/pgdata/socket'" -w start
```

### Django

Из корня проекта подготовьте окружение и скопируйте шаблон настроек при первом запуске:

```sh
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp ../.env.example ../.env
python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

В `../.env` заполните `DJANGO_SECRET_KEY` сгенерированным ключом, а `POSTGRES_PASSWORD` паролем роли `my_cloud`. Значения с пробелами или специальными символами заключайте в одинарные кавычки. Для подключения к другой базе измените `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_HOST` и `POSTGRES_PORT`.

Загрузите настройки и примените миграции из `backend/`:

```sh
set -a
source ../.env
set +a
python manage.py check
python manage.py migrate
```

Миграции создают неактивную учётную запись `admin` с неиспользуемым паролем. Для её активации введите пароль в терминале после команды `read`. Ввод скрыт. Минимум 6 символов, одна заглавная латинская буква, одна цифра и один специальный символ.

```sh
read -r -s MY_CLOUD_ADMIN_PASSWORD
export MY_CLOUD_ADMIN_PASSWORD
python manage.py bootstrap_admin
unset MY_CLOUD_ADMIN_PASSWORD
```

Повторный запуск `bootstrap_admin` сохраняет установленный пароль. Запустите сервер из того же терминала:

```sh
python manage.py runserver 127.0.0.1:8000
```

В другом терминале выполните:

```sh
curl -i http://127.0.0.1:8000/api/health/
```

При доступной базе ответ будет `200` с `{"status":"ok"}`, при недоступной — `503` с `{"status":"unavailable"}`. Уровень логирования задаётся в `DJANGO_LOG_LEVEL`, по умолчанию `INFO`.

### Файловое хранилище

`FILE_STORAGE_ROOT` задаёт корневой каталог. По умолчанию это `private_storage/` в корне проекта. Можно указать абсолютный путь или путь относительно корня проекта. Для развёртывания выбирайте каталог вне публичных каталогов веб-сервера.

Модель `StoredFile` хранит метаданные в PostgreSQL, а содержимое файла лежит на диске по пути `<папка владельца>/<UUID файла без дефисов>`. Оригинальное имя хранится отдельно. Django создаёт каталоги при первой загрузке. Удаление владельца через Django каскадно удаляет записи, физическую очистку добавим вместе с удалением файлов.

`POST /api/files/` принимает один файл в поле `file` и необязательный `comment` через `multipart/form-data`. Запрос требует действующую сессию и заголовок `X-CSRFToken`. `FILE_UPLOAD_MAX_BYTES` задаёт максимальный размер одного файла, по умолчанию 25 МиБ (26 214 400 байт).

`GET /api/files/` возвращает массив файлов текущего пользователя от новых к старым. Обе операции возвращают поля `id`, `original_name`, `comment`, `size_bytes`, `created_at`, `last_downloaded_at`. Пустой список возвращается как `[]`.

### API аккаунта

- `GET /api/auth/csrf/` устанавливает cookie `csrftoken` и возвращает `csrf_token` для заголовка `X-CSRFToken`.
- `POST /api/auth/register/` принимает `username`, `full_name`, `email`, `password` и возвращает `201` с `id`, `username`, `full_name`, `email`.
- `POST /api/auth/login/` принимает `username` и `password`, создаёт сессию и возвращает `200` с данными пользователя, включая `is_staff`.
- `GET /api/auth/me/` возвращает те же данные по действующей сессии.
- `POST /api/auth/logout/` завершает сессию и возвращает `200` с сообщением.

Отправляйте JSON и `X-CSRFToken` с cookie `csrftoken` для всех трёх POST-запросов с того же origin. Django обновляет CSRF-cookie при входе. Перед следующим POST используйте новое значение cookie или снова запросите `GET /api/auth/csrf/`. Cookie сессии `sessionid` имеет флаг `HttpOnly`. При запуске через HTTPS установите `DJANGO_SECURE_COOKIES=1`.

Ошибки в данных возвращаются со статусом `400` и сообщениями по полям. Неверные учётные данные, отсутствие действующей сессии для `me` или `logout` и отказ CSRF возвращают `403` с сообщением в `detail`.

Для тестов используется отдельная база PostgreSQL. При настройках из `.env.example` создайте `test_my_cloud` от имени администратора PostgreSQL командой `createdb --owner=my_cloud test_my_cloud`, затем из `backend/` с загруженными настройками выполните:

```sh
python manage.py test --keepdb
```

### React

Установка зависимостей, сборка и запуск фронтенда из корня проекта:

```sh
cd frontend
npm ci
npm run build
npm run dev -- --strictPort
```

Главная страница открывается по адресу `http://localhost:5173`. Результат сборки находится в `frontend/dist/`.

## Задание

[Исходное техническое задание](docs/ASSIGNMENT_ORIGINAL.md).
