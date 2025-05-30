# Telegram-бот для публикации постов ВКонтакте

Этот бот предназначен для автоматизации процесса публикации и управления сообщениями в группах ВКонтакте через Telegram.

## Основные возможности

- 📝 Публикация постов на стенах групп ВКонтакте
- 🔄 Настройка периодической публикации постов
- 📊 Управление несколькими группами ВКонтакте
- 💬 Отправка и автоматическое удаление сообщений в беседах и чатах
- 📋 Просмотр списка доступных групп и бесед
- 🔑 Простое изменение токена ВКонтакте через интерфейс бота

## Настройка и запуск

### Предварительные требования

- Python 3.7 или выше
- Токен Telegram бота (уже встроен в код)
- Токен API ВКонтакте с правами на управление группами и стеной (добавляется через интерфейс бота)

### Установка

1. Склонируйте репозиторий или скачайте все файлы
2. Установите необходимые зависимости:
   ```
   pip install -r requirements.txt
   ```

### Запуск бота

```bash
python app.py
```

После запуска, найдите своего бота в Telegram и отправьте ему команду `/start`, чтобы начать работу.

## Использование бота

### Настройка токена VK API

После запуска бота первым шагом нужно настроить токен API ВКонтакте:

1. В главном меню выберите "🔑 Сменить токен VK"
2. Введите ваш токен API ВКонтакте
3. Бот подтвердит успешное сохранение токена

### Основное меню

После запуска бота и отправки команды `/start` вы увидите главное меню с кнопками:

- **Добавить чат** - добавление группы или беседы для отправки сообщений
- **Список чатов** - просмотр списка доступных чатов
- **Изменить сообщение** - настройка текста сообщений
- **Изменить время** - настройка тайминга для публикации
- **Сменить токен VK** - обновление токена API ВКонтакте
- **Группы** - меню управления группами ВКонтакте
  - **Опубликовать пост** - публикация поста на стене группы
  - **Периодический пост** - настройка регулярной публикации
  - **Остановить периодику** - прекращение регулярной публикации

### Публикация постов

1. Выберите "Опубликовать пост" в меню
2. Выберите группу из списка
3. Введите текст поста
4. Бот опубликует пост и предоставит ссылку на него

### Периодические посты

1. Выберите "Периодический пост" в меню
2. Выберите группу из списка
3. Введите текст поста и установите интервал
4. Бот будет автоматически публиковать посты с указанным интервалом

## Деплой на сервер

Для деплоя бота на сервер следуйте инструкциям в файле [DEPLOY.md](DEPLOY.md).

## Вопросы и поддержка

Если у вас возникли вопросы по использованию бота или проблемы с его работой, обратитесь ко мне в тг @Makidami1. 
