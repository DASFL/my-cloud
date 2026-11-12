# My Cloud

My Cloud — дипломный проект по курсу «Fullstack-разработчик на Python». Это небольшое облачное хранилище, где пользователь сможет загружать файлы, скачивать их и делиться ссылками.

Сейчас в проекте есть каркас Django и главная страница React с заглушкой «My Cloud — в разработке».

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

Для проверки нужны Python 3.12 и Node.js 24 с npm.

Проверка Django из корня проекта:

```sh
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py check
```

Установка зависимостей, сборка и запуск фронтенда из корня проекта:

```sh
cd frontend
npm ci
npm run build
npm run dev -- --strictPort
```

Главная страница открывается по адресу `http://localhost:5173`. Результат сборки находится в `frontend/dist/`.

Подключение PostgreSQL и запуск серверной части появятся после настройки базы данных.

## Задание

[Исходное техническое задание](docs/ASSIGNMENT_ORIGINAL.md).
