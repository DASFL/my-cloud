# My Cloud

My Cloud — дипломный проект по курсу «Fullstack-разработчик на Python». Это небольшое облачное хранилище, где пользователь сможет загружать файлы, скачивать их и делиться ссылками.

Сейчас в проекте есть каркас Django, проверка доступности PostgreSQL и главная страница React с заглушкой «My Cloud — в разработке».

## Что планируется сделать

- Регистрация и вход в аккаунт.
- Загрузка, скачивание, удаление и переименование файлов.
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

Загрузите настройки и запустите Django из `backend/`:

```sh
set -a
source ../.env
set +a
python manage.py check
python manage.py runserver 127.0.0.1:8000
```

В другом терминале выполните:

```sh
curl -i http://127.0.0.1:8000/api/health/
```

При доступной базе ответ будет `200` с `{"status":"ok"}`, при недоступной — `503` с `{"status":"unavailable"}`. Первый запуск миграций выполняется после добавления собственной модели пользователя. Уровень логирования задаётся в `DJANGO_LOG_LEVEL`, по умолчанию `INFO`.

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
