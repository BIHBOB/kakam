import os
import threading
import time
import signal
import sys
import logging
import uuid
import requests
import json
from typing import Optional, Dict, Any, List
from telebot import TeleBot
from telebot import types

try:
    import vk_api
    from vk_api.exceptions import ApiError
except ImportError as e:
    logging.error(f"Ошибка импорта vk_api: {e}")
    sys.exit(1)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Проверка наличия токенов
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '').strip()
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN не задан или содержит пробелы")
    raise ValueError("TELEGRAM_TOKEN отсутствует или некорректен")

VK_TOKEN = os.getenv('VK_TOKEN', '').strip()
if not VK_TOKEN:
    logger.warning("VK_TOKEN не задан, спам не будет работать до настройки через бота")

# Инициализация VK API
vk = None
try:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    # Проверка токена
    vk.users.get()
    logger.info("VK API успешно инициализирован")
except ApiError as e:
    logger.error(f"Ошибка проверки VK токена: {str(e)}")
    vk = None
except Exception as e:
    logger.error(f"Ошибка инициализации VK API: {str(e)}")
    vk = None

INSTANCE_ID = str(uuid.uuid4())
logger.info(f"Запущен экземпляр бота с ID: {INSTANCE_ID}")

bot = TeleBot(TELEGRAM_TOKEN, threaded=False)

vk_session = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
vk = vk_session.get_api() if vk_session else None

# Проверка токена VK при запуске
if vk:
    try:
        vk.account.getInfo()
        logger.info("VK токен действителен")
    except vk_api.exceptions.ApiError as e:
        logger.error(f"Ошибка проверки VK токена: {str(e)}")
        vk = None

VK_Groups = []
VK_CONVERSATIONS = []
DELAY_TIME = 60  # задержка между сообщениями в секундах
DELETE_TIME = 10  # время до удаления сообщения в секундах
SPAM_RUNNING = {'groups': False, 'conversations': False}
SPAM_THREADS = {'groups': [], 'conversations': []}
SPAM_TEMPLATE = "Привет, это тестовое сообщение от бота!"
bot_started = False

# Глобальные переменные для работы с постами на стене ВКонтакте
LAST_POST_IDS = {}  # Хранение ID последних постов для групп
PERIODIC_TIMERS = {} # Таймеры для периодической отправки постов
PERIODIC_RUNNING = {} # Статус периодической отправки для каждой группы
USER_STATE = {}  # Состояние пользователя для многошаговых операций
POST_TEMPLATE = "Новый пост на стене"  # Шаблон для постов на стене, отдельно от обычных сообщений
pending_posts = {}  # Временное хранение текстов постов

# Глобальная переменная для хранения потоков периодических задач
PERIODIC_THREADS = {}

# Новая система шаблонов с категориями
POST_TEMPLATES = {
    "Общие": [],
    "Рекламные": [],
    "Информационные": []
}
DEFAULT_POST_TEMPLATE_CATEGORY = "Общие"

# Для сохранения настроек
CONFIG_FILE = "bot_config.json"

# Функция для отправки сообщения на стену группы ВКонтакте
def send_message_to_vk_group(group_id: str, message: str, attachments: Optional[list] = None) -> Dict[str, Any]:
    """
    Отправляет сообщение в группу ВКонтакте
    
    Args:
        group_id: ID группы ВКонтакте
        message: Текст сообщения
        attachments: Список вложений (опционально)
        
    Returns:
        Dict с результатом операции
    """
    if not vk:
        logger.error("VK API не инициализирован")
        return {"success": False, "error": "VK API не инициализирован"}
        
    try:
        # Подготовка параметров для поста
        params = {
            "owner_id": f"-{group_id}",
            "message": message,
            "from_group": 1
        }
        
        # Добавляем вложения, если они есть
        if attachments:
            params["attachments"] = ",".join(attachments)
            
        # Публикуем пост
        result = vk.wall.post(**params)
        
        if "post_id" in result:
            logger.info(f"Сообщение успешно опубликовано в группе {group_id}")
            return {"success": True, "post_id": result["post_id"]}
        else:
            logger.error(f"Не удалось опубликовать сообщение в группе {group_id}")
            return {"success": False, "error": "Не удалось получить post_id"}
            
    except ApiError as e:
        error_msg = f"Ошибка VK API при публикации в группе {group_id}: {str(e)}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg}
    except Exception as e:
        error_msg = f"Неожиданная ошибка при публикации в группе {group_id}: {str(e)}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg}

def start_periodic_messages(group_id, message, delay_millis, telegram_chat_id=None):
    """Запускает периодическую отправку сообщений в группу VK"""
    group_id_str = str(group_id)
    
    # Останавливаем предыдущую отправку, если была
    if group_id_str in PERIODIC_RUNNING and PERIODIC_RUNNING[group_id_str]:
        stop_periodic_messages(group_id)
    
    PERIODIC_RUNNING[group_id_str] = True
    
    def periodic_sender():
        sent_count = 0
        while PERIODIC_RUNNING.get(group_id_str, False):
            try:
                # Публикуем пост на стену
                post_id = send_message_to_vk_group(group_id, message)
                
                if post_id:
                    sent_count += 1
                    # Формируем ссылку на пост
                    post_link = get_vk_post_link(group_id, post_id)
                    
                    # Отправляем информацию пользователю только каждые 5 постов
                    if telegram_chat_id and sent_count % 5 == 0:
                        bot.send_message(telegram_chat_id, 
                                      f"✅ Периодическая отправка #{sent_count} успешна!\n"
                                      f"ID поста: {post_id}\n"
                                      f"Ссылка: {post_link}", 
                                      disable_notification=True)
                else:
                    logger.error(f"Не удалось опубликовать периодический пост в группу {group_id}")
                    if telegram_chat_id:
                        bot.send_message(telegram_chat_id, 
                                       f"❌ Не удалось опубликовать периодический пост в группу {group_id}",
                                       disable_notification=True)
            except Exception as e:
                logger.error(f"Ошибка при периодической публикации в группу {group_id}: {str(e)}")
                if telegram_chat_id:
                    bot.send_message(telegram_chat_id, 
                                    f"❌ Ошибка при периодической публикации в группу {group_id}: {str(e)}")
                # При ошибке останавливаем периодическую отправку
                PERIODIC_RUNNING[group_id_str] = False
                break
            
            # Ждем указанный интервал
            time.sleep(delay_millis / 1000)  # интервал в мс, sleep принимает секунды
    
    # Запускаем периодическую отправку в отдельном потоке
    thread = threading.Thread(target=periodic_sender)
    thread.daemon = True  # Поток будет автоматически завершен при завершении основного потока
    thread.start()
    
    PERIODIC_THREADS[group_id_str] = thread
    
    logger.info(f"Запущена периодическая отправка в группу {group_id} с интервалом {delay_millis/1000} сек")
    return True

def stop_periodic_messages(group_id):
    """Останавливает периодическую отправку сообщений в группу VK"""
    group_id_str = str(group_id)
    
    if group_id_str in PERIODIC_RUNNING:
        PERIODIC_RUNNING[group_id_str] = False
        
        # Ждем завершения потока
        if group_id_str in PERIODIC_THREADS and PERIODIC_THREADS[group_id_str].is_alive():
            PERIODIC_THREADS[group_id_str].join(1)  # Ждем максимум 1 секунду
            del PERIODIC_THREADS[group_id_str]
        
        logger.info(f"Остановлена периодическая отправка в группу {group_id}")
        return True
    
    return False

def main_menu():
    """Создает основное меню бота"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    # Основные функции спама и постов
    markup.row(
        types.KeyboardButton("🚀 Спам в группы"), 
        types.KeyboardButton("🚀 Спам в беседы")
    )
    
    # Функции работы с постами
    markup.row(
        types.KeyboardButton("📌 Пост на стену"),
        types.KeyboardButton("🔄 Периодический пост")
    )
    
    # Настройки и управление
    markup.row(
        types.KeyboardButton("⏳ Задержка"), 
        types.KeyboardButton("🕒 Время удаления"),
        types.KeyboardButton("⏹ Остановить периодику")
    )
    
    # Управление чатами
    markup.row(
        types.KeyboardButton("➕ Добавить чат"),
        types.KeyboardButton("🗑 Удалить чат")
    )
    
    # Информация и продвинутые функции
    markup.row(
        types.KeyboardButton("ℹ️ Статус"),
        types.KeyboardButton("⚙️ Настройки")
    )
    
    return markup

def spam_menu(spam_type):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    # Кнопка остановки спама
    markup.row(types.KeyboardButton("⛔ Отключить спам"))
    
    # Функции работы с постами во время спама
    markup.row(
        types.KeyboardButton("📌 Пост на стену"),
        types.KeyboardButton("🔄 Периодический пост")
    )
    
    # Настройки и управление
    markup.row(
        types.KeyboardButton("⏳ Задержка"), 
        types.KeyboardButton("🕒 Время удаления")
    )
    
    # Стандартные функции
    markup.row(
        types.KeyboardButton("ℹ️ Статус"),
        types.KeyboardButton("➕ Добавить чат")
    )
    
    # Управление ВК
    markup.row(
        types.KeyboardButton("✍️ Шаблон для спама"),
        types.KeyboardButton("🔑 Сменить токен VK"),
        types.KeyboardButton("🗑 Очистить API VK")
    )
    
    return markup

# Создаем меню настроек
def settings_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    markup.row(
        types.KeyboardButton("✍️ Шаблон для спама"),
        types.KeyboardButton("📝 Шаблон для постов")
    )
    
    markup.row(
        types.KeyboardButton("🔑 Сменить токен VK"),
        types.KeyboardButton("🔔 Уведомления")
    )
    
    markup.row(
        types.KeyboardButton("📊 Статистика"),
        types.KeyboardButton("🗑 Очистить API VK")
    )
    
    markup.row(
        types.KeyboardButton("🏠 Главное меню")
    )
    
    return markup

# Создаем меню управления
def control_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    markup.row(
        types.KeyboardButton("⏹ Остановить всё"),
        types.KeyboardButton("⏹ Остановить периодику")
    )
    
    markup.row(
        types.KeyboardButton("🔄 Перезагрузить"),
        types.KeyboardButton("💾 Сохранить состояние")
    )
    
    markup.row(
        types.KeyboardButton("📤 Экспорт настроек"),
        types.KeyboardButton("📥 Импорт настроек")
    )
    
    markup.row(
        types.KeyboardButton("🏠 Главное меню")
    )
    
    return markup

def create_remove_chat_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    if VK_Groups or VK_CONVERSATIONS:
        for group_id in VK_Groups:
            markup.add(types.InlineKeyboardButton(f"Группа {group_id}", callback_data=f"remove_group_{group_id}"))
        for conv_id in VK_CONVERSATIONS:
            markup.add(types.InlineKeyboardButton(f"Беседа {conv_id}", callback_data=f"remove_conversation_{conv_id}"))
        markup.add(types.InlineKeyboardButton("Отмена", callback_data="cancel_remove"))
    else:
        markup.add(types.InlineKeyboardButton("Нет чатов для удаления", callback_data="no_chats"))
    return markup

def send_and_delete_vk_messages(chat_id, telegram_chat_id):
    """Отправка и удаление сообщений в VK"""
    try:
        if not vk:
            bot.send_message(telegram_chat_id, "❌ VK токен не установлен или недействителен!")
            return

        # Отправляем сообщение
        message = vk.messages.send(
            peer_id=chat_id,
            message=SPAM_TEMPLATE,
            random_id=0
        )
        
        # Ждем указанное время
        time.sleep(DELETE_TIME)
        
        # Удаляем сообщение
        vk.messages.delete(
            message_ids=message['message_id'],
            delete_for_all=1
        )
        
        bot.send_message(telegram_chat_id, f"✅ Сообщение отправлено и удалено в чат {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка при отправке/удалении сообщения: {str(e)}")
        bot.send_message(telegram_chat_id, f"❌ Ошибка при отправке/удалении сообщения: {str(e)}")

def ping_service():
    """Функция для поддержания работоспособности бота"""
    global bot_started
    PING_INTERVAL = 300  # 5 минут
    
    while bot_started:
        try:
            time.sleep(PING_INTERVAL)
        except Exception as e:
            logger.error(f"Ошибка в ping_service: {str(e)}")
            continue

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Обработчик команды /start"""
    try:
        logger.info(f"Пользователь {message.chat.id} запустил бота")
        bot.send_message(message.chat.id, 
                        f"👋 Привет! Я бот для управления постами ВКонтакте.\n\n"
                        f"🔑 Для начала работы необходимо настроить токен VK API.\n"
                        f"📝 Используйте кнопку '🔑 Сменить токен VK' в меню настроек.\n\n"
                        f"ℹ️ Экземпляр бота: {INSTANCE_ID}", 
                        reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике /start: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при запуске бота. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "🏠 Главное меню")
def main_menu_command(message):
    """Возвращает в главное меню"""
    try:
        bot.send_message(message.chat.id, "🏠 Главное меню:", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике главного меню: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "⏳ Задержка")
def set_delay_prompt(message):
    """Установка задержки между действиями"""
    try:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("15 сек", callback_data="delay_15"),
            types.InlineKeyboardButton("30 сек", callback_data="delay_30"),
            types.InlineKeyboardButton("1 мин", callback_data="delay_60"),
            types.InlineKeyboardButton("5 мин", callback_data="delay_300")
        )
        bot.send_message(message.chat.id, "⏳ Выберите время между действиями:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка в обработчике задержки: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "🕒 Время удаления")
def set_delete_time_prompt(message):
    """Установка времени до удаления сообщений"""
    try:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("5 сек", callback_data="delete_5"),
            types.InlineKeyboardButton("10 сек", callback_data="delete_10"),
            types.InlineKeyboardButton("30 сек", callback_data="delete_30"),
            types.InlineKeyboardButton("1 мин", callback_data="delete_60")
        )
        bot.send_message(message.chat.id, "🕒 Выберите время до удаления сообщений:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка в обработчике времени удаления: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "➕ Добавить чат")
def add_chat_prompt(message):
    """Добавление нового чата"""
    try:
        bot.send_message(message.chat.id, 
                        "✍️ Введите ID чата (группы или беседы):\n"
                        "Для групп используйте отрицательное число\n"
                        "Для бесед используйте положительное число")
        bot.register_next_step_handler(message, process_add_chat)
    except Exception as e:
        logger.error(f"Ошибка в обработчике добавления чата: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "🗑 Удалить чат")
def remove_chat_prompt(message):
    """Удаление чата"""
    try:
        if not VK_Groups and not VK_CONVERSATIONS:
            bot.send_message(message.chat.id, "❌ Список чатов пуст!", reply_markup=main_menu())
            return
        
        markup = create_remove_chat_keyboard()
        bot.send_message(message.chat.id, "🗑 Выберите чат для удаления:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка в обработчике удаления чата: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "ℹ️ Статус")
def show_status(message):
    """Отображение текущего статуса"""
    try:
        status_text = f"📊 Статус бота:\n\n"
        status_text += f"🔑 VK токен: {'✅ Установлен' if vk else '❌ Не установлен'}\n"
        status_text += f"👥 Группы: {len(VK_Groups)} шт.\n"
        status_text += f"💬 Беседы: {len(VK_CONVERSATIONS)} шт.\n"
        status_text += f"⏳ Задержка: {DELAY_TIME} сек\n"
        status_text += f"🕒 Время удаления: {DELETE_TIME} сек\n"
        status_text += f"📝 Шаблон спама: {SPAM_TEMPLATE[:50]}...\n"
        status_text += f"📌 Шаблон поста: {POST_TEMPLATE[:50]}...\n"
        
        # Проверяем активные процессы
        active_periodic = [gid for gid, running in PERIODIC_RUNNING.items() if running]
        if active_periodic:
            status_text += f"\n🔄 Активные периодические отправки: {len(active_periodic)} шт.\n"
        
        if SPAM_RUNNING['groups'] or SPAM_RUNNING['conversations']:
            status_text += "\n🚀 Активный спам:\n"
            if SPAM_RUNNING['groups']:
                status_text += "- В группах\n"
            if SPAM_RUNNING['conversations']:
                status_text += "- В беседах\n"
        
        bot.send_message(message.chat.id, status_text, reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка в обработчике статуса: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

def process_add_chat(message):
    """Обработка добавления нового чата"""
    try:
        chat_id = int(message.text)
        if chat_id < 0 and chat_id not in VK_Groups:
            VK_Groups.append(chat_id)
            bot.send_message(message.chat.id, f"✅ Группа {chat_id} добавлена!", reply_markup=main_menu())
        elif chat_id > 0 and chat_id not in VK_CONVERSATIONS:
            VK_CONVERSATIONS.append(chat_id)
            bot.send_message(message.chat.id, f"✅ Беседа {chat_id} добавлена!", reply_markup=main_menu())
        else:
            bot.send_message(message.chat.id, "❌ Этот чат уже добавлен!", reply_markup=main_menu())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат ID чата!", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка при добавлении чата: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data.startswith("delay_"))
def set_delay_callback(call):
    """Обработчик установки задержки"""
    try:
        global DELAY_TIME
        DELAY_TIME = int(call.data.split("_")[1])
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"✅ Задержка между действиями установлена: {DELAY_TIME} сек", 
                            reply_markup=None)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка в обработчике задержки: {str(e)}")
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="❌ Произошла ошибка. Попробуйте снова.", reply_markup=None)
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
def set_delete_time_callback(call):
    """Обработчик установки времени удаления"""
    try:
        global DELETE_TIME
        DELETE_TIME = int(call.data.split("_")[1])
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"✅ Время до удаления установлено: {DELETE_TIME} сек", 
                            reply_markup=None)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка в обработчике времени удаления: {str(e)}")
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="❌ Произошла ошибка. Попробуйте снова.", reply_markup=None)
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_"))
def handle_remove_chat(call):
    """Обработчик удаления чата"""
    try:
        if call.data == "cancel_remove":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                text="❌ Удаление чата отменено.", reply_markup=None)
            bot.answer_callback_query(call.id)
            return
        
        _, chat_type, chat_id = call.data.split("_")
        chat_id = int(chat_id)
        
        if chat_type == "group" and chat_id in VK_Groups:
            VK_Groups.remove(chat_id)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                text=f"✅ Группа {chat_id} удалена!", reply_markup=None)
        elif chat_type == "conversation" and chat_id in VK_CONVERSATIONS:
            VK_CONVERSATIONS.remove(chat_id)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                text=f"✅ Беседа {chat_id} удалена!", reply_markup=None)
        else:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                text="❌ Чат не найден!", reply_markup=None)
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при удалении чата: {str(e)}")
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="❌ Произошла ошибка. Попробуйте снова.", reply_markup=None)
        bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: message.text == "🚀 Спам в группы")
def start_spam_groups(message):
    global SPAM_RUNNING, SPAM_THREADS
    if not VK_Groups:
        bot.send_message(message.chat.id, "❌ Список групп пуст!", reply_markup=main_menu())
        return
    if not vk:
        bot.send_message(message.chat.id, "❌ VK токен не установлен или недействителен!", reply_markup=main_menu())
        return
    try:
        vk.account.getInfo()
    except vk_api.exceptions.ApiError as e:
        bot.send_message(message.chat.id, f"❌ Ошибка VK токена: {str(e)}. Обновите токен!", reply_markup=main_menu())
        return
    
    SPAM_RUNNING['groups'] = True
    SPAM_THREADS['groups'] = []
    logger.info(f"Запуск спама в группы для {message.chat.id}")
    
    # Отправляем сообщение о старте спама
    bot.send_message(message.chat.id, "✅ Спам запущен в группах VK! Отправляю посты на стены...", reply_markup=spam_menu('groups'))
    
    # Запускаем отдельный поток для спама в группы
    def spam_to_groups():
        success_count = 0
        failed_count = 0
        posts_info = {}  # Хранит информацию о последних отправленных постах
        
        # Бесконечный цикл спама, пока не остановят
        while SPAM_RUNNING['groups']:
            for group_id in VK_Groups[:]:
                # Проверяем, не была ли остановлена отправка
                if not SPAM_RUNNING['groups']:
                    bot.send_message(message.chat.id, "⏹ Отправка спама в группы остановлена")
                    break
                
                try:
                    # Если есть предыдущие посты для этой группы - удаляем их
                    if group_id in posts_info and posts_info[group_id]:
                        prev_posts = posts_info[group_id]
                        for prev_post_id in prev_posts:
                            try:
                                # Удаляем предыдущий пост
                                vk.wall.delete(owner_id=group_id if group_id < 0 else -group_id, post_id=prev_post_id)
                                logger.info(f"Удален предыдущий пост {prev_post_id} из группы {group_id}")
                            except Exception as del_error:
                                logger.error(f"Ошибка при удалении поста из группы {group_id}: {str(del_error)}")
                    
                    # Отправляем 2 сообщения подряд на стену группы
                    current_posts = []
                    
                    # Отправляем первое сообщение
                    post_id1 = send_message_to_vk_group(group_id, SPAM_TEMPLATE, message.chat.id)
                    if post_id1:
                        success_count += 1
                        current_posts.append(post_id1)
                        post_link = get_vk_post_link(group_id, post_id1)
                        bot.send_message(message.chat.id, 
                                       f"✅ Отправлен пост #1 в группу {group_id}\n"
                                       f"Ссылка: {post_link}")
                    else:
                        failed_count += 1
                    
                    # Небольшая пауза между отправками (1 секунда)
                    time.sleep(1)
                    
                    # Отправляем второе сообщение
                    post_id2 = send_message_to_vk_group(group_id, SPAM_TEMPLATE, message.chat.id)
                    if post_id2:
                        success_count += 1
                        current_posts.append(post_id2)
                        post_link = get_vk_post_link(group_id, post_id2)
                        bot.send_message(message.chat.id, 
                                       f"✅ Отправлен пост #2 в группу {group_id}\n"
                                       f"Ссылка: {post_link}")
                    else:
                        failed_count += 1
                    
                    # Сохраняем ID постов для последующего удаления
                    posts_info[group_id] = current_posts
                
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Ошибка при отправке спама в группу {group_id}: {str(e)}")
                    bot.send_message(message.chat.id, f"❌ Ошибка при отправке спама в группу {group_id}: {str(e)}")
            
            # Ждем 59 секунд перед следующим циклом
            for i in range(59):
                if not SPAM_RUNNING['groups']:
                    break
                time.sleep(1)
        
        # Отправляем итоговую статистику
        status_text = f"✅ Спам в группы остановлен!\n"
        status_text += f"📊 Статистика:\n"
        status_text += f"- Успешно отправлено: {success_count}\n"
        status_text += f"- Ошибок: {failed_count}"
        
        bot.send_message(message.chat.id, status_text, reply_markup=main_menu())
    
    # Запускаем поток
    thread = threading.Thread(target=spam_to_groups)
    thread.daemon = True
    thread.start()
    SPAM_THREADS['groups'].append(thread)
    logger.debug(f"Поток запущен для спама в группы")

@bot.message_handler(func=lambda message: message.text == "🚀 Спам в беседы")
def start_spam_conversations(message):
    global SPAM_RUNNING, SPAM_THREADS
    if not VK_CONVERSATIONS:
        bot.send_message(message.chat.id, "Список бесед пуст!", reply_markup=main_menu())
        return
    if not vk:
        bot.send_message(message.chat.id, "VK токен не установлен или недействителен!", reply_markup=main_menu())
        return
    try:
        vk.account.getInfo()
    except vk_api.exceptions.ApiError as e:
        bot.send_message(message.chat.id, f"Ошибка VK токена: {str(e)}. Обновите токен!", reply_markup=main_menu())
        return
    SPAM_RUNNING['conversations'] = True
    SPAM_THREADS['conversations'] = []
    logger.info(f"Запуск спама в беседы для {message.chat.id}")
    for chat_id in VK_CONVERSATIONS[:]:
        thread = threading.Thread(target=send_and_delete_vk_messages, args=(chat_id, message.chat.id))
        thread.daemon = True
        thread.start()
        SPAM_THREADS['conversations'].append(thread)
        logger.debug(f"Поток запущен для беседы {chat_id}")
    bot.send_message(message.chat.id, "Спам запущен в беседах VK!", reply_markup=spam_menu('conversations'))

@bot.message_handler(func=lambda message: message.text == "📌 Пост на стену")
def post_to_wall_prompt(message):
    """Обработчик команды публикации поста на стене группы"""
    if not vk:
        bot.send_message(message.chat.id, "❌ VK токен не установлен или недействителен!", reply_markup=main_menu())
        return
    
    if not VK_Groups:
        bot.send_message(message.chat.id, "❌ Сначала добавьте группы с помощью команды 'Добавить чат'!", reply_markup=main_menu())
        return
    
    try:
        vk.account.getInfo()
    except vk_api.exceptions.ApiError as e:
        bot.send_message(message.chat.id, f"❌ Ошибка VK токена: {str(e)}. Обновите токен!", reply_markup=main_menu())
        return
    
    # Создаем клавиатуру с выбором группы
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем опцию выбора всех групп сразу
    markup.add(types.InlineKeyboardButton("🔄 Выбрать все группы", callback_data="post_to_all_groups"))
    
    # Добавляем кнопку множественного выбора
    markup.add(types.InlineKeyboardButton("✅ Выбрать несколько групп", callback_data="multi_group_selection"))
    
    # Добавляем каждую группу отдельно
    for group_id in VK_Groups:
        markup.add(types.InlineKeyboardButton(f"Группа {group_id}", callback_data=f"post_to_group_{group_id}"))
    
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
    
    bot.send_message(message.chat.id, "📌 Выберите группу(ы) для отправки поста:", reply_markup=markup)

# Добавляем новый обработчик для множественного выбора групп
@bot.callback_query_handler(func=lambda call: call.data == "multi_group_selection")
def multi_group_selection(call):
    """Обработчик множественного выбора групп для постов"""
    if not VK_Groups:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                             text="❌ Список групп пуст!", reply_markup=None)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    # Создаем клавиатуру с чекбоксами для каждой группы
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Инициализируем словарь выбранных групп, если его нет
    if 'selected_groups' not in USER_STATE:
        USER_STATE['selected_groups'] = {}
    
    # Если нет состояния для текущего пользователя, создаем его
    if call.message.chat.id not in USER_STATE['selected_groups']:
        USER_STATE['selected_groups'][call.message.chat.id] = []
    
    # Добавляем кнопки для каждой группы
    for group_id in VK_Groups:
        # Проверяем, выбрана ли группа
        is_selected = group_id in USER_STATE['selected_groups'][call.message.chat.id]
        checkbox = "✅" if is_selected else "☑️"
        markup.add(types.InlineKeyboardButton(
            f"{checkbox} Группа {group_id}", 
            callback_data=f"toggle_group_{group_id}"
        ))
    
    # Добавляем кнопки управления
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить выбор", callback_data="confirm_group_selection"),
        types.InlineKeyboardButton("🔄 Сбросить выбор", callback_data="reset_group_selection"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post")
    )
    
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                         text="✅ Выберите группы для отправки поста (нажмите на группу для выбора/отмены):", 
                         reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_group_"))
def toggle_group_selection(call):
    """Обработчик переключения выбора группы"""
    group_id = int(call.data.split("_")[2])
    
    # Инициализируем словарь, если его нет
    if 'selected_groups' not in USER_STATE:
        USER_STATE['selected_groups'] = {}
    
    # Если нет состояния для текущего пользователя, создаем его
    if call.message.chat.id not in USER_STATE['selected_groups']:
        USER_STATE['selected_groups'][call.message.chat.id] = []
    
    # Переключаем состояние выбора группы
    if group_id in USER_STATE['selected_groups'][call.message.chat.id]:
        USER_STATE['selected_groups'][call.message.chat.id].remove(group_id)
    else:
        USER_STATE['selected_groups'][call.message.chat.id].append(group_id)
    
    # Обновляем клавиатуру с чекбоксами
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем кнопки для каждой группы
    for gid in VK_Groups:
        # Проверяем, выбрана ли группа
        is_selected = gid in USER_STATE['selected_groups'][call.message.chat.id]
        checkbox = "✅" if is_selected else "☑️"
        markup.add(types.InlineKeyboardButton(
            f"{checkbox} Группа {gid}", 
            callback_data=f"toggle_group_{gid}"
        ))
    
    # Добавляем кнопки управления
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить выбор", callback_data="confirm_group_selection"),
        types.InlineKeyboardButton("🔄 Сбросить выбор", callback_data="reset_group_selection"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post")
    )
    
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                         text="✅ Выберите группы для отправки поста (нажмите на группу для выбора/отмены):", 
                         reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "reset_group_selection")
def reset_group_selection(call):
    """Обработчик сброса выбора групп"""
    # Очищаем список выбранных групп
    if 'selected_groups' in USER_STATE and call.message.chat.id in USER_STATE['selected_groups']:
        USER_STATE['selected_groups'][call.message.chat.id] = []
    
    # Обновляем клавиатуру
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем кнопки для каждой группы
    for gid in VK_Groups:
        markup.add(types.InlineKeyboardButton(
            f"☑️ Группа {gid}", 
            callback_data=f"toggle_group_{gid}"
        ))
    
    # Добавляем кнопки управления
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить выбор", callback_data="confirm_group_selection"),
        types.InlineKeyboardButton("🔄 Сбросить выбор", callback_data="reset_group_selection"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post")
    )
    
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                         text="✅ Выберите группы для отправки поста (нажмите на группу для выбора/отмены):", 
                         reply_markup=markup)
    bot.answer_callback_query(call.id, text="Выбор сброшен")

@bot.callback_query_handler(func=lambda call: call.data == "confirm_group_selection")
def confirm_group_selection(call):
    """Обработчик подтверждения выбора групп"""
    # Проверяем, есть ли выбранные группы
    if ('selected_groups' not in USER_STATE or 
        call.message.chat.id not in USER_STATE['selected_groups'] or 
        not USER_STATE['selected_groups'][call.message.chat.id]):
        bot.answer_callback_query(call.id, text="❌ Не выбрано ни одной группы")
        return
    
    # Получаем список выбранных групп
    selected_groups = USER_STATE['selected_groups'][call.message.chat.id]
    
    # Запрашиваем текст поста
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                         text=f"✍️ Введите текст для поста в выбранные группы ({len(selected_groups)} шт.):", 
                         reply_markup=None)
    
    # Сохраняем выбранные группы в USER_STATE для использования в process_multi_group_post
    bot.register_next_step_handler(call.message, process_multi_group_post, selected_groups)
    bot.answer_callback_query(call.id)

# Обработчик для отправки во все группы сразу
@bot.callback_query_handler(func=lambda call: call.data == "post_to_all_groups")
def post_to_all_groups(call):
    """Обработчик выбора всех групп для публикации поста"""
    if not VK_Groups:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                             text="❌ Список групп пуст!", reply_markup=None)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    # Запрашиваем текст поста
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                         text=f"✍️ Введите текст для поста во все группы ({len(VK_Groups)} шт.):", 
                         reply_markup=None)
    
    # Сохраняем все группы для использования в process_multi_group_post
    bot.register_next_step_handler(call.message, process_multi_group_post, list(VK_Groups))
    bot.answer_callback_query(call.id)

# Функция обработки текста поста для нескольких групп
def process_multi_group_post(message: types.Message, group_ids: List[int], delay_millis: int = 0):
    """
    Обрабатывает публикацию поста в несколько групп ВКонтакте
    
    Args:
        message: Объект сообщения Telegram
        group_ids: Список ID групп ВКонтакте
        delay_millis: Задержка между публикациями в миллисекундах
    """
    try:
        # Проверяем инициализацию VK API
        if not vk:
            bot.send_message(message.chat.id, "❌ VK API не инициализирован")
            return

        # Проверяем наличие групп
        if not group_ids:
            bot.send_message(message.chat.id, "❌ Список групп пуст")
            return

        # Отправляем сообщение о начале публикации
        status_message = bot.send_message(
            message.chat.id,
            f"🔄 Начинаю публикацию в {len(group_ids)} групп...\n"
            f"✅ Успешно: 0\n"
            f"❌ Ошибок: 0"
        )

        success_count = 0
        error_count = 0
        error_groups = []

        # Публикуем в каждую группу
        for group_id in group_ids:
            try:
                # Публикуем пост
                result = send_message_to_vk_group(group_id, message.text)
                
                if result.get('success'):
                    success_count += 1
                    post_link = get_vk_post_link(group_id, result['post_id'])
                    bot.send_message(message.chat.id, 
                                   f"✅ Пост опубликован в группе {group_id}\n"
                                   f"Ссылка: {post_link}")
                else:
                    error_count += 1
                    error_groups.append(str(group_id))
                    bot.send_message(message.chat.id,
                                   f"❌ Ошибка при публикации в группу {group_id}: {result.get('error')}")

                # Если есть задержка и это не последняя группа
                if delay_millis > 0 and group_id != group_ids[-1]:
                    time.sleep(delay_millis / 1000)

            except Exception as e:
                error_count += 1
                error_groups.append(str(group_id))
                error_msg = f"Ошибка при публикации в группу {group_id}: {str(e)}"
                logger.error(error_msg)
                bot.send_message(message.chat.id, f"❌ {error_msg}")

        # Формируем итоговое сообщение
        result_message = (
            f"📊 Результаты публикации:\n"
            f"✅ Успешно опубликовано: {success_count}\n"
            f"❌ Ошибок: {error_count}"
        )

        if error_groups:
            result_message += f"\n\n❌ Ошибки в группах: {', '.join(error_groups)}"

        # Создаем клавиатуру для возврата в главное меню
        menu_keyboard = types.InlineKeyboardMarkup()
        menu_keyboard.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"))

        # Отправляем итоговое сообщение
        bot.edit_message_text(
            result_message,
            chat_id=message.chat.id,
            message_id=status_message.message_id,
            reply_markup=menu_keyboard
        )

    except Exception as e:
        error_message = f"❌ Ошибка при публикации: {str(e)}"
        logger.error(error_message)
        bot.send_message(message.chat.id, error_message)

@bot.callback_query_handler(func=lambda call: call.data.startswith("post_to_group_") or call.data == "cancel_post")
def handle_post_to_group_selection(call):
    """Обработчик выбора группы для публикации поста"""
    if call.data == "cancel_post":
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="❌ Отправка поста отменена.", reply_markup=None)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    group_id = int(call.data.split("_")[3])
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                        text=f"✍️ Введите текст для поста в группу {group_id}:", reply_markup=None)
    bot.answer_callback_query(call.id)
    
    # Регистрируем обработчик следующего шага для получения текста поста
    bot.register_next_step_handler(call.message, process_post_text, group_id)

# Функция для получения ссылки на пост ВК
def get_vk_post_link(group_id, post_id):
    """
    Формирует ссылку на пост в ВК
    @param group_id ID группы (с -, для корректности)
    @param post_id ID поста
    @return строка со ссылкой на пост
    """
    group_id_abs = abs(group_id)
    return f"https://vk.com/wall-{group_id_abs}_{post_id}"

# Обновляем функцию обработки текста поста
def process_post_text(message, group_id):
    """Обработка текста поста"""
    try:
        # Проверяем наличие текста
        if not message.text:
            bot.send_message(message.chat.id, "❌ Текст поста не может быть пустым! Попробуйте снова.")
            return

        # Сохраняем текст поста
        post_text = message.text
        
        # Создаем клавиатуру для выбора режима публикации
        markup = types.InlineKeyboardMarkup(row_width=2)
        
        # Если это одиночная группа
        if isinstance(group_id, int):
            # Сохраняем текст поста для одиночной группы
            pending_posts[str(group_id)] = post_text
            markup.add(
                types.InlineKeyboardButton("1️⃣ Одиночный пост", callback_data=f"single_post_{group_id}"),
                types.InlineKeyboardButton("🔄 Множественная отправка", callback_data=f"multi_post_setup_{group_id}")
            )
        # Если это список групп
        elif isinstance(group_id, list):
            # Создаем строку с ID групп для callback_data
            groups_str = "_".join(map(str, group_id))
            # Сохраняем текст поста для множественной отправки
            pending_posts[f"multi_groups_{groups_str}"] = post_text
            markup.add(
                types.InlineKeyboardButton("1️⃣ Одиночный пост", callback_data=f"multi_single_post_{groups_str}"),
                types.InlineKeyboardButton("🔄 Множественная отправка", callback_data=f"multi_multiple_post_{groups_str}")
            )
        
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
        
        # Отправляем сообщение с выбором режима публикации
        bot.send_message(
            message.chat.id,
            "📊 Выберите режим публикации:",
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Ошибка при обработке текста поста: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при обработке текста поста")

@bot.callback_query_handler(func=lambda call: call.data.startswith("single_post_"))
def handle_single_post(call):
    """Обработчик отправки одиночного поста"""
    try:
        # Получаем ID группы
        group_id = int(call.data.replace("single_post_", ""))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(str(group_id))
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Отправляем пост
        post_id = send_message_to_vk_group(group_id, post_text, call.message.chat.id)
        
        if post_id:
            # Получаем ссылку на пост
            post_link = get_vk_post_link(group_id, post_id)
            
            # Создаем клавиатуру для удаления поста
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("❌ Удалить пост", callback_data=f"delete_post_{group_id}_{post_id}"),
                types.InlineKeyboardButton("✅ Оставить", callback_data="keep_post")
            )
            
            # Отправляем сообщение с результатом
            bot.edit_message_text(
                f"✅ Пост успешно опубликован!\nСсылка: {post_link}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup
            )
            
            # Очищаем сохраненный текст
            if str(group_id) in pending_posts:
                del pending_posts[str(group_id)]
        else:
            bot.edit_message_text(
                "❌ Не удалось опубликовать пост",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ошибка при публикации поста: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при публикации поста")

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_post_setup_"))
def handle_multi_post_setup(call):
    """Обработчик настройки множественной отправки постов"""
    try:
        # Получаем ID группы
        group_id = int(call.data.replace("multi_post_setup_", ""))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(str(group_id))
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру для выбора количества постов
        markup = types.InlineKeyboardMarkup(row_width=3)
        
        # Добавляем кнопки выбора количества
        row1 = []
        row2 = []
        
        for i in range(1, 11):
            row1.append(types.InlineKeyboardButton(f"{i}", callback_data=f"post_count_{group_id}_{i}"))
        for i in range(15, 55, 5):
            row2.append(types.InlineKeyboardButton(f"{i}", callback_data=f"post_count_{group_id}_{i}"))
        
        markup.add(*row1)
        markup.add(*row2)
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="🔢 Выберите количество постов (от 1 до 50):",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при настройке множественной отправки: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при настройке отправки")

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_single_post_"))
def handle_multi_single_post(call):
    """Обработчик отправки одиночного поста в несколько групп"""
    try:
        # Получаем список групп из callback_data
        groups = [int(gid) for gid in call.data.replace("multi_single_post_", "").split("_")]
        groups_str = "_".join(map(str, groups))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(f"multi_groups_{groups_str}")
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру с кнопкой остановки
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("⏹ Остановить отправку", callback_data=f"stop_multi_posts_{groups[0]}"))
        
        # Отправляем сообщение о начале отправки
        bot.edit_message_text(
            f"✅ Начинаю отправку поста в {len(groups)} групп",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
        )
        
        # Запускаем отправку постов в отдельном потоке
        thread = threading.Thread(
            target=send_to_multiple_groups,
            args=(groups, post_text, call.message.chat.id)
        )
        thread.daemon = True
        thread.start()
        
        # Сохраняем информацию о потоке
        if 'multiple_posts_threads' not in globals():
            global multiple_posts_threads
            multiple_posts_threads = {}
        multiple_posts_threads[f"multi_{groups[0]}"] = {
            'thread': thread,
            'running': True
        }
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ошибка при отправке поста: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при отправке поста")

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_multiple_post_"))
def handle_multi_multiple_post(call):
    """Обработчик настройки множественной отправки постов в несколько групп"""
    try:
        # Получаем список групп из callback_data
        groups = [int(gid) for gid in call.data.replace("multi_multiple_post_", "").split("_")]
        groups_str = "_".join(map(str, groups))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(f"multi_groups_{groups_str}")
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру для выбора количества постов
        markup = types.InlineKeyboardMarkup(row_width=3)
        
        # Добавляем кнопки выбора количества
        row1 = []
        row2 = []
        
        for i in range(1, 11):
            row1.append(types.InlineKeyboardButton(f"{i}", callback_data=f"multi_post_count_{groups_str}_{i}"))
        for i in range(15, 55, 5):
            row2.append(types.InlineKeyboardButton(f"{i}", callback_data=f"multi_post_count_{groups_str}_{i}"))
        
        markup.add(*row1)
        markup.add(*row2)
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔢 Выберите количество постов для отправки в {len(groups)} групп (от 1 до 50):",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при настройке множественной отправки: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при настройке отправки")

@bot.callback_query_handler(func=lambda call: call.data.startswith("post_count_"))
def handle_post_count_selection(call):
    """Обработчик выбора количества постов для множественной отправки"""
    try:
        # Извлекаем параметры
        parts = call.data.split("_")
        group_id = int(parts[2])
        post_count = int(parts[3])
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(str(group_id))
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру для выбора интервала
        markup = types.InlineKeyboardMarkup(row_width=3)
        
        # Добавляем кнопки выбора интервала (в минутах)
        intervals = [1, 2, 3, 5, 10, 15, 30, 60]
        buttons = []
        
        for interval in intervals:
            interval_text = f"{interval} мин" if interval > 1 else f"{interval} мин"
            buttons.append(types.InlineKeyboardButton(
                interval_text,
                callback_data=f"post_interval_{group_id}_{post_count}_{interval}"
            ))
        
        # Разбиваем кнопки на ряды по 3
        for i in range(0, len(buttons), 3):
            markup.add(*buttons[i:i+3])
        
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"⏱ Выберите интервал публикации {post_count} постов (в минутах):",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при выборе количества постов: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при выборе количества")

@bot.callback_query_handler(func=lambda call: call.data.startswith("post_interval_"))
def handle_post_interval_selection(call):
    """Обработчик выбора интервала для множественной отправки постов"""
    try:
        # Извлекаем параметры
        parts = call.data.split("_")
        group_id = int(parts[2])
        post_count = int(parts[3])
        interval = int(parts[4])
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(str(group_id))
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру с кнопкой остановки
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("⏹ Остановить отправку", callback_data=f"stop_multiple_posts_{group_id}"))
        
        # Отправляем сообщение о начале отправки
        bot.edit_message_text(
            f"✅ Начинаю отправку {post_count} постов с интервалом {interval} минут",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
        )
        
        # Запускаем отправку постов в отдельном потоке
        thread = threading.Thread(
            target=send_multiple_posts,
            args=(group_id, post_text, post_count, interval * 60, call.message.chat.id)
        )
        thread.daemon = True
        thread.start()
        
        # Сохраняем информацию о потоке
        if 'multiple_posts_threads' not in globals():
            global multiple_posts_threads
            multiple_posts_threads = {}
        multiple_posts_threads[group_id] = {
            'thread': thread,
            'running': True
        }
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при выборе интервала: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при выборе интервала")

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_multiple_posts_"))
def handle_stop_multiple_posts(call):
    """Обработчик остановки отправки нескольких постов"""
    try:
        group_id = int(call.data.split('_')[3])
        
        if 'multiple_posts_threads' in globals() and group_id in multiple_posts_threads:
            # Останавливаем поток
            multiple_posts_threads[group_id]['running'] = False
            thread = multiple_posts_threads[group_id]['thread']
            
            # Ждем завершения потока
            if thread.is_alive():
                thread.join(timeout=5)
            
            # Удаляем информацию о потоке
            del multiple_posts_threads[group_id]
            
            bot.edit_message_text(
                f"✅ Отправка постов в группу {group_id} остановлена",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )
        else:
            bot.answer_callback_query(call.id, "❌ Поток отправки не найден")
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при остановке отправки постов: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при остановке отправки")

def send_multiple_posts(group_id, post_text, post_count, interval_seconds, telegram_chat_id):
    """Отправка множества постов в одну группу"""
    success_count = 0
    failed_count = 0
    
    try:
        # Отправляем статус начала
        bot.send_message(telegram_chat_id, f"📊 Начинаю отправку {post_count} постов в группу {group_id}")
        
        # Отправляем посты с интервалом
        for i in range(post_count):
            # Проверяем, не была ли остановлена отправка
            if not multiple_posts_threads.get(group_id, {}).get('running', False):
                bot.send_message(telegram_chat_id, f"⏹ Отправка постов в группу {group_id} остановлена")
                break
            
            try:
                # Отправляем пост
                post_id = send_message_to_vk_group(group_id, post_text, telegram_chat_id)
                
                if post_id:
                    success_count += 1
                    post_link = get_vk_post_link(group_id, post_id)
                    bot.send_message(telegram_chat_id, 
                                   f"✅ Пост {i+1}/{post_count} опубликован\nСсылка: {post_link}")
                else:
                    failed_count += 1
                    bot.send_message(telegram_chat_id, f"❌ Не удалось опубликовать пост {i+1}/{post_count}")
                
                # Если это не последний пост, ждем указанный интервал
                if i < post_count - 1:
                    time.sleep(interval_seconds)
                
            except Exception as post_error:
                failed_count += 1
                logger.error(f"Ошибка при публикации поста {i+1}: {str(post_error)}")
                bot.send_message(telegram_chat_id, f"❌ Ошибка при публикации поста {i+1}/{post_count}")
        
        # Отправляем итоговую статистику
        status_text = f"✅ Отправка завершена!\n"
        status_text += f"📊 Статистика:\n"
        status_text += f"- Успешно: {success_count}/{post_count}\n"
        status_text += f"- Ошибки: {failed_count}/{post_count}"
        
        bot.send_message(telegram_chat_id, status_text, reply_markup=main_menu())
        
    except Exception as e:
        logger.error(f"Глобальная ошибка при отправке постов: {str(e)}")
        bot.send_message(telegram_chat_id, f"❌ Произошла ошибка: {str(e)}", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main_menu(call):
    """Возврат в главное меню по кнопке"""
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                        text=call.message.text + "\n\n✅ Операция завершена", 
                        reply_markup=None)
    bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_post_") or call.data == "keep_post")
def handle_post_deletion(call):
    """Обработчик удаления опубликованного поста"""
    if call.data == "keep_post":
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="✅ Пост оставлен на стене.", reply_markup=None)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    # Извлекаем параметры
    _, _, group_id, post_id = call.data.split("_")
    group_id = int(group_id)
    post_id = int(post_id)
    
    # Проверяем, отрицательное ли значение group_id (для сообществ)
    if group_id > 0:
        group_id = -group_id
    
    try:
        # Удаляем пост
        vk.wall.delete(owner_id=group_id, post_id=post_id)
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"🗑 Пост {post_id} успешно удален с стены группы {abs(group_id)}.", reply_markup=None)
        
        # Удаляем ID поста из списка сохраненных, если он там есть
        if group_id in LAST_POST_IDS and post_id in LAST_POST_IDS[group_id]:
            LAST_POST_IDS[group_id].remove(post_id)
            logger.info(f"Удален пост {post_id} из сохраненных для группы {group_id}")
    except vk_api.exceptions.ApiError as e:
        error_msg = f"❌ Ошибка при удалении поста {post_id}: {str(e)}"
        if "Access denied" in str(e) or "access denied" in str(e).lower():
            error_msg += "\nВозможно, у вас нет прав на удаление этого поста или пост уже был удален."
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=error_msg, reply_markup=None)
        logger.error(error_msg)
    except Exception as e:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"❌ Неизвестная ошибка при удалении поста: {str(e)}", reply_markup=None)
        logger.error(f"Неизвестная ошибка при удалении поста {post_id} из группы {group_id}: {str(e)}")
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda message: message.text == "⏹ Остановить периодику")
def stop_all_periodic_prompt(message):
    """Обработчик команды остановки всех периодических отправок"""
    active_groups = [group_id for group_id, running in PERIODIC_RUNNING.items() if running]
    
    if not active_groups:
        bot.send_message(message.chat.id, "ℹ️ Нет активных периодических отправок.", reply_markup=main_menu())
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем кнопки для каждой активной группы
    for group_id in active_groups:
        markup.add(types.InlineKeyboardButton(f"Остановить группу {group_id}", callback_data=f"stop_periodic_{group_id}"))
    
    # Добавляем кнопку для остановки всех групп
    markup.add(types.InlineKeyboardButton("⏹ Остановить все", callback_data="stop_all_periodic"))
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_stop_periodic"))
    
    bot.send_message(message.chat.id, "Выберите группу для остановки периодической отправки:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_periodic_") or 
                                             call.data == "stop_all_periodic" or 
                                             call.data == "cancel_stop_periodic")
def handle_stop_periodic(call):
    """Обработчик остановки периодических отправок"""
    if call.data == "cancel_stop_periodic":
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text="❌ Остановка периодической отправки отменена.", reply_markup=None)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    if call.data == "stop_all_periodic":
        stopped_count = 0
        for group_id, running in list(PERIODIC_RUNNING.items()):
            if running:
                stop_periodic_messages(int(group_id))
                stopped_count += 1
        
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"✅ Остановлены все периодические отправки ({stopped_count})", reply_markup=None)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())
        return
    
    # Обработка остановки для конкретной группы
    group_id = int(call.data.split("_")[2])
    if stop_periodic_messages(group_id):
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"✅ Остановлена периодическая отправка для группы {group_id}", reply_markup=None)
    else:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                            text=f"❌ Не удалось остановить отправку для группы {group_id} или она уже не активна", 
                            reply_markup=None)
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda message: message.text == "⛔ Отключить спам")
def stop_spam(message):
    """Обработчик команды остановки спама в группах и беседах"""
    global SPAM_RUNNING, SPAM_THREADS
    
    stopped_count = 0
    
    # Останавливаем спам в группах
    if SPAM_RUNNING['groups']:
        SPAM_RUNNING['groups'] = False
        stopped_count += 1
        logger.info(f"Остановка спама в группах для {message.chat.id}")
        bot.send_message(message.chat.id, "⏹ Останавливаю отправку спама в группы...")
    
    # Останавливаем спам в беседах
    if SPAM_RUNNING['conversations']:
        SPAM_RUNNING['conversations'] = False
        stopped_count += 1
        logger.info(f"Остановка спама в беседах для {message.chat.id}")
        bot.send_message(message.chat.id, "⏹ Останавливаю отправку спама в беседы...")
    
    # Ожидаем завершения потоков спама
    for thread_type in SPAM_THREADS:
        threads = SPAM_THREADS[thread_type][:]
        for thread in threads:
            if thread.is_alive():
                logger.debug(f"Ожидание завершения потока для {thread_type}")
                thread.join(timeout=5)
                if thread.is_alive():
                    logger.warning(f"Поток для {thread_type} не завершился вовремя")
        SPAM_THREADS[thread_type].clear()
    
    if stopped_count > 0:
        bot.send_message(message.chat.id, "✅ Спам остановлен!", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, "ℹ️ Нет активного спама для остановки.", reply_markup=main_menu())

@bot.message_handler(func=lambda message: message.text == "⚙️ Настройки")
def settings_command(message):
    """Отображает меню настроек"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    markup.row(
        types.KeyboardButton("✍️ Шаблон для спама"),
        types.KeyboardButton("📝 Шаблон для постов")
    )
    
    markup.row(
        types.KeyboardButton("🔑 Сменить токен VK"),
        types.KeyboardButton("🔔 Уведомления")
    )
    
    markup.row(
        types.KeyboardButton("📊 Статистика"),
        types.KeyboardButton("🗑 Очистить API VK")
    )
    
    markup.row(
        types.KeyboardButton("🏠 Главное меню")
    )
    
    bot.send_message(message.chat.id, "⚙️ Меню настроек:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "🛠 Управление")
def control_command(message):
    """Отображает меню управления"""
    bot.send_message(message.chat.id, "🛠 Меню управления:", reply_markup=control_menu())

@bot.message_handler(func=lambda message: message.text == "✍️ Шаблон для спама")
def set_spam_template(message):
    """Установка шаблона для спама"""
    try:
        bot.send_message(message.chat.id, "✍️ Введите новый шаблон для спама:")
        bot.register_next_step_handler(message, process_spam_template)
    except Exception as e:
        logger.error(f"Ошибка в обработчике шаблона спама: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "📝 Шаблон для постов")
def set_post_template(message):
    """Установка шаблона для постов"""
    try:
        bot.send_message(message.chat.id, "✍️ Введите новый шаблон для постов:")
        bot.register_next_step_handler(message, process_post_template)
    except Exception as e:
        logger.error(f"Ошибка в обработчике шаблона постов: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "🔑 Сменить токен VK")
def update_vk_token_prompt(message):
    """Запрос нового токена VK"""
    try:
        bot.send_message(message.chat.id, "🔑 Введите новый токен VK API:")
        bot.register_next_step_handler(message, update_vk_token)
    except Exception as e:
        logger.error(f"Ошибка в обработчике обновления токена: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "🗑 Очистить API VK")
def clear_vk_api(message):
    """Очистка API VK"""
    try:
        global vk, vk_session
        vk = None
        vk_session = None
        bot.send_message(message.chat.id, "✅ API VK очищен. Используйте '🔑 Сменить токен VK' для установки нового токена.")
    except Exception as e:
        logger.error(f"Ошибка при очистке API VK: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

@bot.message_handler(func=lambda message: message.text == "⏹ Остановить всё")
def stop_all(message):
    """Остановка всех процессов"""
    try:
        global SPAM_RUNNING, SPAM_THREADS, PERIODIC_RUNNING
        
        # Останавливаем спам
        SPAM_RUNNING['groups'] = False
        SPAM_RUNNING['conversations'] = False
        
        # Останавливаем периодические отправки
        for group_id in list(PERIODIC_RUNNING.keys()):
            stop_periodic_messages(int(group_id))
        
        # Ожидаем завершения потоков
        for thread_type in SPAM_THREADS:
            threads = SPAM_THREADS[thread_type][:]
            for thread in threads:
                if thread.is_alive():
                    thread.join(timeout=5)
            SPAM_THREADS[thread_type].clear()
        
        bot.send_message(message.chat.id, "✅ Все процессы остановлены!", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка при остановке всех процессов: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

def process_spam_template(message):
    """Обработка нового шаблона для спама"""
    try:
        global SPAM_TEMPLATE
        SPAM_TEMPLATE = message.text
        bot.send_message(message.chat.id, "✅ Шаблон для спама обновлен!", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка при обновлении шаблона спама: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

def process_post_template(message):
    """Обработка нового шаблона для постов"""
    try:
        global POST_TEMPLATE
        POST_TEMPLATE = message.text
        bot.send_message(message.chat.id, "✅ Шаблон для постов обновлен!", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка при обновлении шаблона постов: {str(e)}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка. Попробуйте снова.")

def update_vk_token(message):
    """Обновление токена VK"""
    try:
        global vk, vk_session, VK_TOKEN
        new_token = message.text.strip()
        
        # Проверяем токен
        test_session = vk_api.VkApi(token=new_token)
        test_api = test_session.get_api()
        test_api.account.getInfo()
        
        # Если токен валиден, обновляем глобальные переменные
        VK_TOKEN = new_token
        vk_session = test_session
        vk = test_api
        
        bot.send_message(message.chat.id, "✅ Токен VK успешно обновлен!", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Ошибка при обновлении токена VK: {str(e)}")
        bot.send_message(message.chat.id, "❌ Неверный токен VK. Попробуйте снова.")

def send_to_multiple_groups(groups, post_text, telegram_chat_id):
    """Функция для отправки поста в несколько групп"""
    success_count = 0
    failed_groups = []
    
    try:
        # Отправляем статус начала
        bot.send_message(telegram_chat_id, f"📊 Начинаю отправку поста в {len(groups)} групп")
        
        # Отправляем пост в каждую группу
        for group_id in groups:
            # Проверяем, не была ли остановлена отправка
            if not multiple_posts_threads.get(f"multi_{groups[0]}", {}).get('running', False):
                bot.send_message(telegram_chat_id, "⏹ Отправка постов остановлена")
                break
            
            try:
                # Отправляем пост на стену группы
                post_id = send_message_to_vk_group(group_id, post_text, telegram_chat_id)
                
                if post_id:
                    success_count += 1
                    post_link = get_vk_post_link(group_id, post_id)
                    bot.send_message(telegram_chat_id, 
                                   f"✅ Пост опубликован в группе {group_id}\nСсылка: {post_link}")
                else:
                    failed_groups.append(group_id)
                    bot.send_message(telegram_chat_id, f"❌ Не удалось опубликовать пост в группе {group_id}")
            
            except Exception as post_error:
                logger.error(f"Ошибка при публикации поста в группу {group_id}: {str(post_error)}")
                failed_groups.append(group_id)
                bot.send_message(telegram_chat_id, f"❌ Ошибка при публикации поста в группу {group_id}")
        
        # Статистика в конце
        status_text = f"✅ Отправка завершена!\n"
        status_text += f"📊 Статистика:\n"
        status_text += f"- Успешно: {success_count}/{len(groups)}\n"
        status_text += f"- Ошибки: {len(groups) - success_count}/{len(groups)}"
        
        if failed_groups:
            status_text += f"\n\n❌ Не удалось отправить в группы: {', '.join(map(str, failed_groups))}"
        
        bot.send_message(telegram_chat_id, status_text, reply_markup=main_menu())
        
    except Exception as e:
        logger.error(f"Глобальная ошибка при отправке постов в группы: {str(e)}")
        bot.send_message(telegram_chat_id, f"❌ Произошла ошибка: {str(e)}", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_post_count_"))
def handle_multi_post_count(call):
    """Обработчик выбора количества постов для множественной отправки в несколько групп"""
    try:
        # Извлекаем параметры из callback_data
        parts = call.data.split("_")
        post_count = int(parts[-1])
        groups = [int(gid) for gid in parts[3:-1]]
        groups_str = "_".join(map(str, groups))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(f"multi_groups_{groups_str}")
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру для выбора интервала
        markup = types.InlineKeyboardMarkup(row_width=3)
        
        # Добавляем кнопки выбора интервала (в минутах)
        intervals = [1, 2, 3, 5, 10, 15, 30, 60]
        buttons = []
        
        for interval in intervals:
            interval_text = f"{interval} мин" if interval > 1 else f"{interval} мин"
            buttons.append(types.InlineKeyboardButton(
                interval_text,
                callback_data=f"multi_post_interval_{groups_str}_{post_count}_{interval}"
            ))
        
        # Разбиваем кнопки на ряды по 3
        for i in range(0, len(buttons), 3):
            markup.add(*buttons[i:i+3])
        
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"⏱ Выберите интервал публикации {post_count} постов в {len(groups)} групп (в минутах):",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при выборе количества постов: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при выборе количества")

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_post_interval_"))
def handle_multi_post_interval(call):
    """Обработчик выбора интервала для множественной отправки в несколько групп"""
    try:
        # Извлекаем параметры из callback_data
        parts = call.data.split("_")
        interval = int(parts[-1])
        post_count = int(parts[-2])
        groups = [int(gid) for gid in parts[3:-2]]
        groups_str = "_".join(map(str, groups))
        
        # Получаем сохраненный текст поста
        post_text = pending_posts.get(f"multi_groups_{groups_str}")
        if not post_text:
            bot.answer_callback_query(call.id, "❌ Текст поста не найден")
            return
        
        # Создаем клавиатуру с кнопкой остановки
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("⏹ Остановить отправку", callback_data=f"stop_multiple_posts_{groups[0]}"))
        
        # Отправляем сообщение о начале отправки
        bot.edit_message_text(
            f"✅ Начинаю отправку {post_count} постов в {len(groups)} групп с интервалом {interval} минут",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
        )
        
        # Запускаем отправку постов в отдельном потоке
        thread = threading.Thread(
            target=send_multiple_posts_to_groups,
            args=(groups, post_text, post_count, interval * 60, call.message.chat.id)
        )
        thread.daemon = True
        thread.start()
        
        # Сохраняем информацию о потоке
        if 'multiple_posts_threads' not in globals():
            global multiple_posts_threads
            multiple_posts_threads = {}
        multiple_posts_threads[f"multi_{groups[0]}"] = {
            'thread': thread,
            'running': True
        }
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Ошибка при выборе интервала: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при выборе интервала")

def send_multiple_posts_to_groups(groups, post_text, post_count, interval_seconds, telegram_chat_id):
    """Отправка множества постов в несколько групп"""
    success_count = 0
    failed_count = 0
    total_posts = post_count * len(groups)
    current_post = 0
    
    try:
        # Отправляем статус начала
        bot.send_message(telegram_chat_id, 
                        f"📊 Начинаю отправку {post_count} постов в {len(groups)} групп")
        
        # Отправляем посты с интервалом
        for i in range(post_count):
            # Проверяем, не была ли остановлена отправка
            if not multiple_posts_threads.get(f"multi_{groups[0]}", {}).get('running', False):
                bot.send_message(telegram_chat_id, "⏹ Отправка постов остановлена")
                break
            
            # Отправляем пост в каждую группу
            for group_id in groups:
                try:
                    current_post += 1
                    
                    # Отправляем пост
                    post_id = send_message_to_vk_group(group_id, post_text, telegram_chat_id)
                    
                    if post_id:
                        success_count += 1
                        post_link = get_vk_post_link(group_id, post_id)
                        
                        # Отправляем уведомление каждые 5 постов или если это последний пост
                        if current_post % 5 == 0 or current_post == total_posts:
                            bot.send_message(telegram_chat_id, 
                                           f"✅ Опубликовано {current_post}/{total_posts}\n"
                                           f"Последний пост: {post_link}")
                    else:
                        failed_count += 1
                        bot.send_message(telegram_chat_id, 
                                       f"❌ Не удалось опубликовать пост {current_post}/{total_posts} "
                                       f"в группу {group_id}")
                
                except Exception as post_error:
                    failed_count += 1
                    logger.error(f"Ошибка при публикации поста в группу {group_id}: {str(post_error)}")
                    bot.send_message(telegram_chat_id, 
                                   f"❌ Ошибка при публикации поста {current_post}/{total_posts} "
                                   f"в группу {group_id}")
            
            # Если это не последний пост, ждем указанный интервал
            if i < post_count - 1:
                time.sleep(interval_seconds)
        
        # Отправляем итоговую статистику
        status_text = f"✅ Отправка завершена!\n"
        status_text += f"📊 Статистика:\n"
        status_text += f"- Успешно: {success_count}/{total_posts}\n"
        status_text += f"- Ошибки: {failed_count}/{total_posts}"
        
        bot.send_message(telegram_chat_id, status_text, reply_markup=main_menu())
        
    except Exception as e:
        logger.error(f"Глобальная ошибка при отправке постов: {str(e)}")
        bot.send_message(telegram_chat_id, f"❌ Произошла ошибка: {str(e)}", reply_markup=main_menu())

# Запуск бота через polling
if __name__ == '__main__':
    try:
        logger.info("Запуск бота через polling...")
        # Запускаем бота через polling
        bot.infinity_polling(timeout=20, long_polling_timeout=5)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {str(e)}")
        bot_started = False
        raise
