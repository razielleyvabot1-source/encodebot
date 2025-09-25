import os
import logging
import asyncio
import threading
import concurrent.futures
import tempfile
import json
from pyrogram import Client, filters
import random
import string
import datetime
import subprocess
from pyrogram.types import (Message, InlineKeyboardButton, 
                           InlineKeyboardMarkup, ReplyKeyboardMarkup, 
                           KeyboardButton, CallbackQuery)
from pyrogram.errors import MessageNotModified
import ffmpeg
import re
import time
from pymongo import MongoClient
from config import *
from bson.objectid import ObjectId
import uuid

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Diccionario de prioridades por plan (ahora solo para límites de cola)
PLAN_PRIORITY = {
    "ultra": 0,  
    "premium": 1,
    "pro": 2,
    "standard": 3
}

# Límite de cola para usuarios premium
PREMIUM_QUEUE_LIMIT = 3
ULTRA_QUEUE_LIMIT = 10

# Conexión a MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DATABASE_NAME]
pending_col = db["pending"]
users_col = db["users"]
temp_keys_col = db["temp_keys"]
banned_col = db["banned_users"]
pending_confirmations_col = db["pending_confirmations"]
active_compressions_col = db["active_compressions"]
user_settings_col = db["user_settings"]  # Nueva colección para configuraciones de usuario

# Configuración del bot
api_id = API_ID
api_hash = API_HASH
bot_token = BOT_TOKEN

app = Client(
    "compress_bot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

# Administradores del bot
admin_users = ADMINS_IDS
ban_users = []

# Cargar usuarios baneados y limpiar compresiones activas al iniciar
banned_users_in_db = banned_col.find({}, {"user_id": 1})
for banned_user in banned_users_in_db:
    if banned_user["user_id"] not in ban_users:
        ban_users.append(banned_user["user_id"])

# Limpiar compresiones activas previas al iniciar
active_compressions_col.delete_many({})
logger.info("Compresiones activas previas eliminadas")

# Configuración de compresión de video (configuración global por defecto)
DEFAULT_VIDEO_SETTINGS = {
    'resolution': '854x480',
    'crf': '28',
    'audio_bitrate': '64k',
    'fps': '22',
    'preset': 'veryfast',
    'codec': 'libx264'
}

# Variables globales para la cola - MODIFICADO: Ahora permite hasta 3 compresiones simultáneas
compression_queue = asyncio.Queue()
processing_tasks = []  # Lista para almacenar múltiples tareas de procesamiento
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# ======================== SISTEMA MEJORADO DE GESTIÓN DE COMPRESIONES ======================== #
# MODIFICADO: Ahora usamos compression_id único para cada compresión

# Diccionarios indexados por compression_id en lugar de user_id
cancel_tasks = {}  # {compression_id: task_info}
ffmpeg_processes = {}  # {compression_id: process}
active_messages = {}  # {compression_id: message_id}

# ======================== NUEVO: SISTEMA DE MONITOREO EN TIEMPO REAL ======================== #
# Diccionario para almacenar información de progreso en tiempo real
compression_progress = {}  # {compression_id: progress_data}

# Diccionario para almacenar configuraciones temporales durante el flujo personalizado
temp_custom_settings = {}

# Valores disponibles para personalización
CUSTOM_CRF_OPTIONS = ['25', '28', '30', '32', '35', '38', '40']
CUSTOM_FPS_OPTIONS = ['20', '22', '25', '28', '30', '35']
CUSTOM_AUDIO_OPTIONS = ['64k', '70k', '80k', '90k', '128k']

def get_crf_keyboard(selected_crf=None):
    """Genera teclado para selección de CRF con opción seleccionada marcada"""
    buttons = []
    row = []
    
    for crf in CUSTOM_CRF_OPTIONS:
        text = f"✔️ {crf}" if selected_crf == crf else crf
        row.append(InlineKeyboardButton(text, callback_data=f"custom_crf_{crf}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    # Botones de navegación - MODIFICADO: Regresar a la izquierda, Siguiente a la derecha
    nav_buttons = []
    # Primero agregar el botón de regresar (izquierda)
    nav_buttons.append(InlineKeyboardButton("🔙 Regresar", callback_data="back_to_settings"))
    
    # Luego agregar el botón de siguiente (derecha) si hay un CRF seleccionado
    if selected_crf:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="custom_next_fps"))
    
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_fps_keyboard(selected_fps=None):
    """Genera teclado para selección de FPS con opción seleccionada marcada"""
    buttons = []
    row = []
    
    for fps in CUSTOM_FPS_OPTIONS:
        text = f"✔️ {fps}" if selected_fps == fps else fps
        row.append(InlineKeyboardButton(text, callback_data=f"custom_fps_{fps}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    # Botones de navegación
    nav_buttons = [
        InlineKeyboardButton("🔙 Atrás", callback_data="custom_back_crf")
    ]
    if selected_fps:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data="custom_next_audio"))
    
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

def get_audio_keyboard(selected_audio=None):
    """Genera teclado para selección de audio con opción seleccionada marcada"""
    buttons = []
    row = []
    
    for audio in CUSTOM_AUDIO_OPTIONS:
        text = f"✔️ {audio}" if selected_audio == audio else audio
        row.append(InlineKeyboardButton(text, callback_data=f"custom_audio_{audio}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    # Botones de navegación
    nav_buttons = [
        InlineKeyboardButton("🔙 Atrás", callback_data="custom_back_fps")
    ]
    if selected_audio:
        nav_buttons.append(InlineKeyboardButton("Finalizar ✅", callback_data="custom_finish"))
    
    buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

async def apply_custom_settings(user_id, settings):
    """Aplica la configuración personalizada al usuario"""
    try:
        # Obtener configuración actual del usuario
        current_settings = await get_user_video_settings(user_id)
        
        # Actualizar solo los valores personalizados
        if 'crf' in settings:
            current_settings['crf'] = settings['crf']
        if 'fps' in settings:
            current_settings['fps'] = settings['fps']
        if 'audio_bitrate' in settings:
            current_settings['audio_bitrate'] = settings['audio_bitrate']
        
        # Guardar en la base de datos
        user_settings_col.update_one(
            {"user_id": user_id},
            {"$set": {"video_settings": current_settings}},
            upsert=True
        )
        
        logger.info(f"Configuración personalizada aplicada para usuario {user_id}: {settings}")
        return True
    except Exception as e:
        logger.error(f"Error aplicando configuración personalizada: {e}")
        return False

# ======================== NUEVAS FUNCIONES PARA EXPORTACIÓN/IMPORTACIÓN DE DB ======================== #

@app.on_message(filters.command("getdb") & filters.user(admin_users))
async def get_db_command(client, message):
    """Exporta la base de datos de usuarios a un archivo JSON"""
    try:
        # Obtener todos los usuarios
        users = list(users_col.find({}))
        
        # Obtener la cantidad de usuarios
        user_count = len(users)
        
        # Crear un archivo temporal
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
            json.dump(users, tmp_file, default=str, indent=4)
            tmp_file.flush()
            
            # Enviar el archivo con la cantidad de usuarios en el caption
            await message.reply_document(
                document=tmp_file.name,
                caption=f"📊 Copia de la base de datos de usuarios\n👤**Usuarios:** {user_count}"
            )
            
            # Eliminar el archivo temporal
            os.unlink(tmp_file.name)
            
    except Exception as e:
        logger.error(f"Error en get_db_command: {e}", exc_info=True)
        await message.reply("❌ Error al exportar la base de datos")

@app.on_message(filters.command("restdb") & filters.user(admin_users))
async def rest_db_command(client, message):
    """Solicita el archivo JSON para restaurar la base de datos"""
    await message.reply(
        "🔄 **Modo restauración activado**\n\n"
        "Envía el archivo JSON de la base de datos " 
        "que deseas restaurar."
    )

@app.on_message(filters.document & filters.user(admin_users))
async def handle_db_restore(client, message):
    """Maneja la restauración de la base de datos desde un archivo JSON"""
    try:
        # Verificar que sea un archivo JSON
        if not message.document.file_name.endswith('.json'):
            return
            
        # Descargar el archivo
        file_path = await message.download()
        
        # Leer el archivo JSON
        with open(file_path, 'r', encoding='utf-8') as f:
            users_data = json.load(f)
        
        # Validar la estructura del JSON
        if not isinstance(users_data, list):
            await message.reply("❌ El archivo JSON no tiene la estructura correcta.")
            os.remove(file_path)
            return
            
        # Eliminar todos los usuarios actuales
        users_col.delete_many({})
        
        # Insertar los nuevos usuarios
        if users_data:
            # Convertir fechas de string a datetime
            for user in users_data:
                if 'join_date' in user and isinstance(user['join_date'], str):
                    user['join_date'] = datetime.datetime.fromisoformat(user['join_date'])
                if 'expires_at' in user and user['expires_at'] and isinstance(user['expires_at'], str):
                    user['expires_at'] = datetime.datetime.fromisoformat(user['expires_at'])
            
            users_col.insert_many(users_data)
        
        # Eliminar el archivo temporal
        os.remove(file_path)
        
        await message.reply(
            f"✅ **Base de datos restaurada exitosamente**\n\n"
            f"Se restauraron {len(users_data)} usuarios."
        )
        
        logger.info(f"Base de datos restaurada por {message.from_user.id} con {len(users_data)} usuarios")
        
    except json.JSONDecodeError:
        await message.reply("❌ El archivo no es un JSON válido.")
    except Exception as e:
        logger.error(f"Error restaurando base de datos: {e}", exc_info=True)
        await message.reply("❌ Error al restaurar la base de datos.")

# ======================== FUNCIÓN PARA FORMATEAR TIEMPO ======================== #

def format_time(seconds):
    """Formatea segundos a formato HH:MM:SS o MM:SS"""
    if seconds < 0:
        return "00:00"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

# ======================== SISTEMA DE CONFIGURACIÓN POR USUARIO ======================== #

async def get_user_video_settings(user_id: int) -> dict:
    """Obtiene la configuración de video personalizada del usuario o la global por defecto"""
    user_settings = user_settings_col.find_one({"user_id": user_id})
    if user_settings and "video_settings" in user_settings:
        return user_settings["video_settings"]
    return DEFAULT_VIDEO_SETTINGS.copy()

async def update_user_video_settings(user_id: int, command: str):
    """Actualiza la configuración de video personalizada del usuario"""
    try:
        settings = command.split()
        new_settings = {}
        for setting in settings:
            if '=' in setting:
                key, value = setting.split('=', 1)
                # Validar que la configuración es válida
                if key in DEFAULT_VIDEO_SETTINGS:
                    new_settings[key] = value
        
        if new_settings:
            # Actualizar o insertar la configuración del usuario
            user_settings_col.update_one(
                {"user_id": user_id},
                {"$set": {"video_settings": new_settings}},
                upsert=True
            )
            logger.info(f"Configuración actualizada para usuario {user_id}: {new_settings}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error actualizando configuración para usuario {user_id}: {e}", exc_info=True)
        return False

async def reset_user_video_settings(user_id: int):
    """Restablece la configuración del usuario a los valores por defecto"""
    user_settings_col.delete_one({"user_id": user_id})
    logger.info(f"Configuración restablecida para usuario {user_id}")

# ======================== SISTEMA MEJORADO DE CANCELACIÓN ======================== #
# MODIFICADO: Ahora usa compression_id único para cada compresión

def generate_compression_id():
    """Genera un ID único para cada compresión"""
    return str(uuid.uuid4())

def register_cancelable_task(compression_id, task_type, task, original_message_id=None, progress_message_id=None):
    """Registra una tarea que puede ser cancelada usando compression_id único"""
    cancel_tasks[compression_id] = {
        "type": task_type, 
        "task": task, 
        "original_message_id": original_message_id,
        "progress_message_id": progress_message_id
    }

def unregister_cancelable_task(compression_id):
    """Elimina el registro de una tarea cancelable"""
    if compression_id in cancel_tasks:
        del cancel_tasks[compression_id]

def register_ffmpeg_process(compression_id, process):
    """Registra un proceso FFmpeg para una compresión específica"""
    ffmpeg_processes[compression_id] = process

def unregister_ffmpeg_process(compression_id):
    """Elimina el registro de un proceso FFmpeg"""
    if compression_id in ffmpeg_processes:
        del ffmpeg_processes[compression_id]

def cancel_compression_task(compression_id):
    """Cancela una tarea específica de compresión usando compression_id"""
    if compression_id in cancel_tasks:
        task_info = cancel_tasks[compression_id]
        try:
            if task_info["type"] == "download":
                # Para descargas, marcamos para cancelación
                return True
            elif task_info["type"] == "ffmpeg" and compression_id in ffmpeg_processes:
                process = ffmpeg_processes[compression_id]
                if process.poll() is None:
                    process.terminate()
                    # Esperar un poco y forzar kill si es necesario
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
                    return True
            elif task_info["type"] == "upload":
                # Para subidas, marcamos para cancelación
                return True
        except Exception as e:
            logger.error(f"Error cancelando tarea {compression_id}: {e}")
    return False

def get_user_compression_ids(user_id):
    """Obtiene todos los compression_id activos para un usuario"""
    user_compressions = []
    for compression_id, task_info in cancel_tasks.items():
        # Buscar en active_compressions_col para obtener el user_id asociado
        compression_data = active_compressions_col.find_one({"compression_id": compression_id})
        if compression_data and compression_data.get("user_id") == user_id:
            user_compressions.append(compression_id)
    return user_compressions

# ======================== NUEVO: SISTEMA DE MONITOREO DE PROGRESO ======================== #

def update_compression_progress(compression_id, stage, current=0, total=0, percent=0, file_name=""):
    """Actualiza el progreso de una compresión para monitoreo en tiempo real"""
    compression_progress[compression_id] = {
        "stage": stage,  # "descarga", "compresión", "subida"
        "current": current,
        "total": total,
        "percent": percent,
        "file_name": file_name,
        "last_update": time.time()
    }

def remove_compression_progress(compression_id):
    """Elimina el progreso de una compresión completada"""
    if compression_id in compression_progress:
        del compression_progress[compression_id]

def create_mini_progress_bar(percent, bar_length=8):
    """Crea una barra de progreso mini para el monitoreo"""
    try:
        percent = max(0, min(100, percent))
        filled_length = int(bar_length * percent / 100)
        bar = '⬢' * filled_length + '⬡' * (bar_length - filled_length)
        return f"[{bar}] {int(percent)}%"
    except:
        return f"[⬡⬡⬡⬡⬡⬡⬡⬡] {int(percent)}%"

async def get_queue_status(user_id=None):
    """Obtiene el estado actual de la cola con información detallada - MODIFICADO: Misma vista para todos"""
    try:
        # Obtener compresiones activas
        active_compr = list(active_compressions_col.find({}))
        
        # Obtener cola pendiente - MODIFICADO: Siempre obtener toda la cola para agrupar por usuario
        pending_queue = list(pending_col.find().sort("timestamp", 1))
        
        # Contadores
        active_count = len(active_compr)
        pending_count = len(pending_queue)
        max_simultaneous = 1 
        
        # Construir respuesta - MODIFICADO: Misma estructura para todos
        response = "📊 **Estado de la cola**\n\n"
        response += f"💠 **Procesos activos:** {active_count}/{max_simultaneous}\n\n"
        
        # Procesos activos - MISMO FORMATO PARA TODOS
        if active_compr:
            response += "🔄 **Procesos activos:**\n"
            
            for i, comp in enumerate(active_compr, 1):
                compression_id = comp.get("compression_id")
                comp_user_id = comp.get("user_id")
                file_name = comp.get("file_name", "Sin nombre")
                
                # Obtener información del usuario
                try:
                    user = await app.get_users(comp_user_id)
                    username = f"@{user.username}" if user.username else f"Usuario {comp_user_id}"
                except:
                    username = f"Usuario {comp_user_id}"
                
                # Obtener información de progreso en tiempo real
                stage_display = "**🗜️Compresión**"
                progress_bar = "[⬡⬡⬡⬡⬡⬡⬡⬡] 0%"
                
                if compression_id in compression_progress:
                    progress_data = compression_progress[compression_id]
                    stage = progress_data["stage"]
                    percent = progress_data["percent"]
                    
                    # Traducir etapa
                    if stage == "download":
                        stage_display = "**⬇️Descarga**"
                    elif stage == "compression":
                        stage_display = "**🗜️Compresión**"
                    elif stage == "upload":
                        stage_display = "**⬆️Subida**"
                    
                    progress_bar = create_mini_progress_bar(percent)
                
                response += f"{i}. {username} ➧ {progress_bar}\n[{stage_display}]\n"
        else:
            response += "🔄 **Procesos activos:**\n• Ninguno\n"
        
        # Lista de espera - MISMO FORMATO AGRUPADO PARA TODOS
        response += "\n⏳ **En proceso y en cola:**\n"
        if pending_queue:
            # Agrupar por usuario y contar cuántos videos tiene cada uno en cola
            user_queue_count = {}
            for item in pending_queue:
                pending_user_id = item.get("user_id")
                if pending_user_id in user_queue_count:
                    user_queue_count[pending_user_id] += 1
                else:
                    user_queue_count[pending_user_id] = 1
            
            # Mostrar cada usuario con su cantidad de videos
            for i, (pending_user_id, count) in enumerate(user_queue_count.items(), 1):
                # Obtener información del usuario
                try:
                    user = await app.get_users(pending_user_id)
                    username = f"@{user.username}" if user.username else f"Usuario {pending_user_id}"
                except:
                    username = f"Usuario {pending_user_id}"
                
                # Mostrar el usuario una vez con la cantidad de videos que tiene
                response += f"{i}. {username}"
                if count > 1:
                    response += f" ({count} videos)"
                response += "\n"
        else:
            response += "• Ninguno\n"
        
        # Resumen total - MISMO FORMATO PARA TODOS
        unique_active_users = len(set(comp["user_id"] for comp in active_compr))
        unique_pending_users = len(set(item["user_id"] for item in pending_queue))
        
        response += f"\n📈 **Resumen total:**\n"
        response += f"   • **Procesando:** {unique_active_users} usuario{'s' if unique_active_users != 1 else ''}\n"
        response += f"   • **En espera:** {unique_pending_users} usuario{'s' if unique_pending_users != 1 else ''}\n"
        
        # Información adicional solo para administradores
        if user_id in admin_users:
            response += f"\n👑 **Vista de administrador:**\n"
            response += f"• **Total en cola:** {pending_count} video(s)\n"
            response += f"• **Tamaño de cola:** {compression_queue.qsize()}\n"
        
        return response
        
    except Exception as e:
        logger.error(f"Error en get_queue_status: {e}")
        return "❌ **Error al obtener el estado de la cola**"

# Hilo para verificar cancelaciones
def cancellation_checker():
    """Hilo que verifica constantemente las solicitudes de cancelación"""
    while True:
        try:
            for compression_id in list(cancel_tasks.keys()):
                task_info = cancel_tasks[compression_id]
                if task_info["type"] == "ffmpeg" and compression_id in ffmpeg_processes:
                    process = ffmpeg_processes[compression_id]
                    if process.poll() is not None:
                        # Proceso ya terminado, limpiar
                        unregister_cancelable_task(compression_id)
                        unregister_ffmpeg_process(compression_id)
            time.sleep(0.5)  # Verificar cada medio segundo
        except Exception as e:
            logger.error(f"Error en cancellation_checker: {e}")
            time.sleep(1)

# Iniciar hilo de verificación de cancelaciones
cancellation_thread = threading.Thread(target=cancellation_checker, daemon=True)
cancellation_thread.start()

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client, message):
    """Maneja el comando de cancelación - MODIFICADO: Usa compression_id único"""
    user_id = message.from_user.id
    
    # Obtener todas las compresiones activas del usuario
    user_compression_ids = get_user_compression_ids(user_id)
    
    if user_compression_ids:
        # Cancelar todas las compresiones activas del usuario
        canceled_count = 0
        for compression_id in user_compression_ids:
            if cancel_compression_task(compression_id):
                # Obtener información de la tarea antes de desregistrarla
                task_info = cancel_tasks.get(compression_id, {})
                original_message_id = task_info.get("original_message_id")
                progress_message_id = task_info.get("progress_message_id")
                
                # Eliminar mensaje de progreso si existe
                if progress_message_id:
                    try:
                        await app.delete_messages(message.chat.id, progress_message_id)
                        if compression_id in active_messages:
                            del active_messages[compression_id]
                    except Exception as e:
                        logger.error(f"Error eliminando mensaje de progreso: {e}")
                
                # Limpiar registros
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                await remove_active_compression(compression_id)
                remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
                
                canceled_count += 1
        
        if canceled_count > 0:
            # Enviar mensaje de cancelación
            await send_protected_message(
                message.chat.id,
                f"⛔ **{canceled_count} compresión(es) cancelada(s)** ⛔"
            )
        else:
            await send_protected_message(
                message.chat.id,
                "⚠️ **No se pudieron cancelar las operaciones activas**"
            )
    else:
        # Cancelar tareas en cola
        result = pending_col.delete_many({"user_id": user_id})
        if result.deleted_count > 0:
            await send_protected_message(
                message.chat.id,
                f"⛔ **Se cancelaron {result.deleted_count} tareas pendientes en la cola.** ⛔"
            )
        else:
            await send_protected_message(
                message.chat.id,
                "ℹ️ **No tienes operaciones activas ni en cola para cancelar.**"
            )
    
    # Borrar mensaje de comando /cancel
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Error borrando mensaje /cancel: {e}")

# ======================== NUEVA FUNCIÓN PARA CANCELAR VIDEOS EN COLA ======================== #

@app.on_message(filters.command("cancelqueue") & filters.private)
async def cancel_queue_command(client, message):
    """Permite a los usuarios cancelar videos específicos de su cola"""
    try:
        user_id = message.from_user.id
        
        # Verificar si el usuario está baneado
        if user_id in ban_users:
            return
            
        # Verificar si el usuario tiene un plan
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso para usar este bot.**\n\n"
                "💲 Para ver los planes disponibles usa el comando /planes\n\n"
                "👨🏻‍💻 Para más información, contacte a @InfiniteNetworkAdmin."
            )
            return
            
        # Obtener los videos en cola del usuario
        user_queue = list(pending_col.find({"user_id": user_id}).sort("timestamp", 1))
        
        if not user_queue:
            await send_protected_message(
                message.chat.id,
                "📋**No tienes videos en la cola de compresión.**"
            )
            return
            
        # Si no se especifica índice, mostrar la lista de videos en cola
        parts = message.text.split()
        if len(parts) == 1:
            response = "**Tus videos en cola:**\n\n"
            for i, item in enumerate(user_queue, 1):
                file_name = item.get("file_name", "Sin nombre")
                timestamp = item.get("timestamp")
                time_str = timestamp.strftime("%H:%M:%S") if timestamp else "¿?"
                response += f"{i}. `{file_name}` (⏰ {time_str})\n"
                
            response += "\nPara cancelar un video, usa /cancelqueue <número>\n"
            response += "Para cancelar todos, usa /cancelqueue --all"
            
            await send_protected_message(message.chat.id, response)
            return
            
        # Manejar --all para cancelar todos los videos
        if parts[1] == "--all":
            # Primero obtener todos los wait_message_id para eliminar los mensajes
            wait_message_ids = []
            for item in user_queue:
                wait_msg_id = item.get("wait_message_id")
                if wait_msg_id:
                    wait_message_ids.append(wait_msg_id)
            
            result = pending_col.delete_many({"user_id": user_id})
            
            # Intentar eliminar los mensajes de espera
            try:
                if wait_message_ids:
                    await app.delete_messages(chat_id=message.chat.id, message_ids=wait_message_ids)
            except Exception as e:
                logger.error(f"Error eliminando mensajes de espera: {e}")
            
            await send_protected_message(
                message.chat.id,
                f"✅ **Se cancelaron todos los videos de tu cola**\n"
                f"• Videos eliminados: {result.deleted_count}"
            )
            return
            
        # Manejar cancelación de video específico
        try:
            index = int(parts[1]) - 1
            if index < 0 or index >= len(user_queue):
                await send_protected_message(
                    message.chat.id,
                    f"❌ **Número inválido.** Debe ser entre 1 y {len(user_queue)}"
                )
                return
                
            video_to_cancel = user_queue[index]
            wait_message_id = video_to_cancel.get("wait_message_id")
            
            # Eliminar de la base de datos
            pending_col.delete_one({"_id": video_to_cancel["_id"]})
            
            # Intentar eliminar el mensaje de espera
            try:
                if wait_message_id:
                    await app.delete_messages(chat_id=message.chat.id, message_ids=[wait_message_id])
            except Exception as e:
                logger.error(f"Error eliminando mensaje de espera: {e}")
            
            await send_protected_message(
                message.chat.id,
                f"**Video cancelado:** `{video_to_cancel.get('file_name', 'Sin nombre')}`\n\n"
                f"✅ Eliminado de la cola de compresión."
            )
            
        except ValueError:
            await send_protected_message(
                message.chat.id,
                "**Usa** /cancelqueue <número> **o** /cancelqueue --all"
            )
            
    except Exception as e:
        logger.error(f"Error en cancel_queue_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "**Error al procesar la solicitud.**"
        )

# ======================== GESTIÓN MEJORADA DE COMPRESIONES ACTIVAS ======================== #
# MODIFICADO: Ahora usa compression_id único

async def has_active_compression(user_id: int) -> bool:
    """Verifica si el usuario ya tiene una compresión activa"""
    return bool(active_compressions_col.find_one({"user_id": user_id}))

async def add_active_compression(compression_id: str, user_id: int, file_id: str, file_name: str):
    """Registra una nueva compresión activa con ID único"""
    active_compressions_col.insert_one({
        "compression_id": compression_id,
        "user_id": user_id,
        "file_id": file_id,
        "file_name": file_name,  # NUEVO: Guardar nombre del archivo
        "start_time": datetime.datetime.now()
    })

async def remove_active_compression(compression_id: str):
    """Elimina una compresión activa por compression_id"""
    active_compressions_col.delete_one({"compression_id": compression_id})

async def get_active_compressions_count(user_id: int) -> int:
    """Obtiene el número de compresiones activas para un usuario"""
    return active_compressions_col.count_documents({"user_id": user_id})

# ======================== SISTEMA DE CONFIRMACIÓN ======================== #

async def has_pending_confirmation(user_id: int) -> bool:
    """Verifica si el usuario tiene una confirmación pendiente (no expirada)"""
    now = datetime.datetime.now()
    expiration_time = now - datetime.timedelta(minutes=10)
    
    # Eliminar confirmaciones expiradas
    pending_confirmations_col.delete_many({
        "user_id": user_id,
        "timestamp": {"$lt": expiration_time}
    })
    
    # Verificar si queda alguna confirmación activa
    return bool(pending_confirmations_col.find_one({"user_id": user_id}))

async def create_confirmation(user_id: int, chat_id: int, message_id: int, file_id: str, file_name: str):
    """Crea una nueva confirmación pendiente eliminando cualquier confirmación previa"""
    # Eliminar cualquier confirmación previa para el mismo usuario
    pending_confirmations_col.delete_many({"user_id": user_id})
    
    return pending_confirmations_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_id": file_id,
        "file_name": file_name,
        "timestamp": datetime.datetime.now()
    }).inserted_id

async def delete_confirmation(confirmation_id: ObjectId):
    """Elimina una confirmación pendiente"""
    pending_confirmations_col.delete_one({"_id": confirmation_id})

async def get_confirmation(confirmation_id: ObjectId):
    """Obtiene una confirmación pendiente"""
    return pending_confirmations_col.find_one({"_id": confirmation_id})

# ======================== AUTO-REGISTRO DE USUARIOS ======================== #

async def register_new_user(user_id: int):
    """Registra un nuevo usuario si no existe"""
    if not users_col.find_one({"user_id": user_id}):
        logger.info(f"Usuario no registrado: {user_id}")

# ======================== FUNCIONES PROTECCIÓN DE CONTENIDO ======================== #

async def should_protect_content(user_id: int) -> bool:
    """Determina si el contenido debe protegerse según el plan del usuario"""
    if user_id in admin_users:
        return False
    user_plan = await get_user_plan(user_id)
    return user_plan is None or user_plan["plan"] == "standard"

async def send_protected_message(chat_id: int, text: str, **kwargs):
    """Envía un mensaje con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_message(chat_id, text, protect_content=protect, **kwargs)

async def send_protected_video(chat_id: int, video: str, caption: str = None, **kwargs):
    """Envía un video con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_video(chat_id, video, caption=caption, protect_content=protect, **kwargs)

async def send_protected_photo(chat_id: int, photo: str, caption: str = None, **kwargs):
    """Envía una foto con protección según el plan del usuario"""
    protect = await should_protect_content(chat_id)
    return await app.send_photo(chat_id, photo, caption=caption, protect_content=protect, **kwargs)

# ======================== SISTEMA DE LÍMITES DE COLA ======================== #

async def get_user_queue_limit(user_id: int) -> int:
    """Obtiene el límite de cola del usuario basado en su plan"""
    user_plan = await get_user_plan(user_id)
    if user_plan is None:
        return 1  # Límite por defecto para usuarios sin plan
    
    if user_plan["plan"] == "ultra":
        return ULTRA_QUEUE_LIMIT
    return PREMIUM_QUEUE_LIMIT if user_plan["plan"] == "premium" else 1

# ======================== SISTEMA DE CLAVES TEMPORALES ======================== #

def generate_temp_key(plan: str, duration_value: int, duration_unit: str):
    """Genera una clave temporal válida para un plan específico"""
    key = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    created_at = datetime.datetime.now()
    
    # Calcular la expiración basada en la unidad de tiempo
    if duration_unit == 'minutes':
        expires_at = created_at + datetime.timedelta(minutes=duration_value)
    elif duration_unit == 'hours':
        expires_at = created_at + datetime.timedelta(hours=duration_value)
    else:  # días por defecto
        expires_at = created_at + datetime.timedelta(days=duration_value)
    
    temp_keys_col.insert_one({
        "key": key,
        "plan": plan,
        "created_at": created_at,
        "expires_at": expires_at,
        "used": False,
        "duration_value": duration_value,
        "duration_unit": duration_unit
    })
    
    return key

def is_valid_temp_key(key):
    """Verifica si una clave temporal es válida"""
    now = datetime.datetime.now()
    key_data = temp_keys_col.find_one({
        "key": key,
        "used": False,
        "expires_at": {"$gt": now}
    })
    return bool(key_data)

def mark_key_used(key):
    """Marca una clave como usada"""
    temp_keys_col.update_one({"key": key}, {"$set": {"used": True}})

@app.on_message(filters.command("generatekey") & filters.user(admin_users))
async def generate_key_command(client, message):
    """Genera una nueva clave temporal para un plan específico (solo admins)"""
    try:
        parts = message.text.split()
        if len(parts) != 4:
            await message.reply("⚠️ Formato: /generatekey <plan> <cantidad> <unidad>\nEjemplo: /generatekey standard 2 hours\nUnidades válidas: minutes, hours, days")
            return
            
        plan = parts[1].lower()
        valid_plans = ["standard", "pro", "premium"]  # No incluir "ultra" en claves temporales
        if plan not in valid_plans:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(valid_plans)}")
            return
            
        try:
            duration_value = int(parts[2])
            if duration_value <= 0:
                await message.reply("⚠️ La cantidad debe ser un número positivo")
                return
        except ValueError:
            await message.reply("⚠️ La cantidad debe ser un número entero")
            return

        duration_unit = parts[3].lower()
        valid_units = ["minutes", "hours", "days"]
        if duration_unit not in valid_units:
            await message.reply(f"⚠️ Unidad inválida. Opciones válidas: {', '.join(valid_units)}")
            return

        key = generate_temp_key(plan, duration_value, duration_unit)
        
        # Texto para mostrar la duración en formato amigable
        duration_text = f"{duration_value} {duration_unit}"
        if duration_value == 1:
            duration_text = duration_text[:-1]  # Remover la 's' final para singular
        
        await message.reply(
            f"**Clave {plan.capitalize()} generada**\n\n"
            f"Clave: `{key}`\n"
            f"Válida por: {duration_text}\n\n"
            f"Comparte esta clave con el usuario usando:\n"
            f"`/key {key}`"
        )
    except Exception as e:
        logger.error(f"Error generando clave: {e}", exc_info=True)
        await message.reply("⚠️ Error al generar la clave")

@app.on_message(filters.command("listkeys") & filters.user(admin_users))
async def list_keys_command(client, message):
    """Lista todas las claves temporales activas (solo admins)"""
    try:
        now = datetime.datetime.now()
        keys = list(temp_keys_col.find({"used": False, "expires_at": {"$gt": now}}))
        
        if not keys:
            await message.reply("**No hay claves activas.**")
            return
            
        response = "**Claves temporales activas:**\n\n"
        for key in keys:
            expires_at = key["expires_at"]
            remaining = expires_at - now
            
            # Formatear el tiempo restante
            if remaining.days > 0:
                time_remaining = f"{remaining.days}d {remaining.seconds//3600}h"
            elif remaining.seconds >= 3600:
                time_remaining = f"{remaining.seconds//3600}h {(remaining.seconds%3600)//60}m"
            else:
                time_remaining = f"{remaining.seconds//60}m"
            
            # Formatear la duración original
            duration_value = key.get("duration_value", 0)
            duration_unit = key.get("duration_unit", "days")
            
            duration_display = f"{duration_value} {duration_unit}"
            if duration_value == 1:
                duration_display = duration_display[:-1]  # Singular
            
            response += (
                f"• `{key['key']}`\n"
                f"  ↳ Plan: {key['plan'].capitalize()}\n"
                f"  ↳ Duración: {duration_display}\n"
                f"  ⏱ Expira en: {time_remaining}\n\n"
            )
            
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error listando claves: {e}", exc_info=True)
        await message.reply("⚠️ Error al listar claves")

@app.on_message(filters.command("delkeys") & filters.user(admin_users))
async def del_keys_command(client, message):
    """Elimina claves temporales (solo admins)"""
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("⚠️ Formato: /delkeys <key> o /delkeys --all")
            return

        option = parts[1]

        if option == "--all":
            # Eliminar todas las claves
            result = temp_keys_col.delete_many({})
            await message.reply(f"**Se eliminaron {result.deleted_count} claves.**")
        else:
            # Eliminar clave específica
            key = option
            result = temp_keys_col.delete_one({"key": key})
            if result.deleted_count > 0:
                await message.reply(f"✅ **Clave {key} eliminada.**")
            else:
                await message.reply("⚠️ **Clave no encontrada.**")
    except Exception as e:
        logger.error(f"Error eliminando claves: {e}", exc_info=True)
        await message.reply("⚠️ **Error al eliminar claves**")

# ======================== SISTEMA DE PLANES ======================== #

PLAN_DURATIONS = {
    "standard": "7 días",
    "pro": "15 días",
    "premium": "30 días",
    "ultra": "Ilimitado"  
}

async def get_user_plan(user_id: int) -> dict:
    """Obtiene el plan del usuario desde la base de datos y elimina si ha expirado"""
    user = users_col.find_one({"user_id": user_id})
    now = datetime.datetime.now()
    
    if user:
        plan = user.get("plan")
        # Si el plan es None, eliminamos el usuario y retornamos None
        if plan is None:
            users_col.delete_one({"user_id": user_id})
            return None

        # Si tiene plan, verificamos la expiración (excepto para plan ultra)
        if plan != "ultra":  # El plan ultra no expira
            expires_at = user.get("expires_at")
            if expires_at and now > expires_at:
                users_col.delete_one({"user_id": user_id})
                return None

        # Si llegamos aquí, el usuario tiene un plan no nulo y no expirado
        # Actualizar campos si faltan
        update_data = {}
        if "last_used_date" not in user:
            update_data["last_used_date"] = None
        
        if update_data:
            users_col.update_one({"user_id": user_id}, {"$set": update_data})
            user.update(update_data)
        
        return user
        
    return None

async def set_user_plan(user_id: int, plan: str, notify: bool = True, expires_at: datetime = None):
    """Establece el plan de un usuario and notifica si notify=True"""
    if plan not in PLAN_DURATIONS:
        return False
        
    # Para el plan ultra, no establecer fecha de expiración
    if plan == "ultra":
        expires_at = None
    else:
        # Si no se proporciona expires_at, calcularlo según el plan
        if expires_at is None:
            now = datetime.datetime.now()
            if plan == "standard":
                expires_at = now + datetime.timedelta(days=7)
            elif plan == "pro":
                expires_at = now + datetime.timedelta(days=15)
            elif plan == "premium":
                expires_at = now + datetime.timedelta(days=30)

    # Actualizar o insertar el usuario con el plan y la fecha de expiración
    user_data = {
        "plan": plan
    }
    if expires_at is not None:
        user_data["expires_at"] = expires_at

    # Si el usuario no existe, se establecerá join_date en la inserción
    existing_user = users_col.find_one({"user_id": user_id})
    if not existing_user:
        user_data["join_date"] = datetime.datetime.now()

    users_col.update_one(
        {"user_id": user_id},
        {"$set": user_data},
        upsert=True
    )
    
    # Notificar al usuario sobre su nuevo plan solo si notify es True
    if notify:
        try:
            await send_protected_message(
                user_id,
                f"**¡Se te ha asignado un nuevo plan!**\n"
                f"Use el comando /start para iniciar en el bot\n\n"
                f"• **Plan**: {plan.capitalize()}\n"
                f"• **Duración**: {PLAN_DURATIONS[plan]}\n"
                f"• **Videos disponibles**: Ilimitados\n\n"
                f"¡Disfruta de tus beneficios! 🎬"
            )
        except Exception as e:
            logger.error(f"Error notificando al usuario {user_id}: {e}")
    
    return True

async def check_user_limit(user_id: int) -> bool:
    """Verifica si el usuario ha alcanzado su límite de compresión"""
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        return True  # Usuario sin plan no puede comprimir
        
    # Todos los planes tienen compresión ilimitada
    return False

# En la función get_plan_info, modifica el mensaje para incluir el botón
async def get_plan_info(user_id: int) -> str:
    """Obtiene información del plan del usuario para mostrar"""
    user = await get_user_plan(user_id)
    if user is None or user.get("plan") is None:
        # Mensaje modificado para incluir el botón
        return (
            "**No tienes un plan activo.**\n\n⬇️**Toque para ver nuestros planes**⬇️"
        )
    
    plan_name = user["plan"].capitalize()
    
    expires_at = user.get("expires_at")
    expires_text = "No expira"
    
    if isinstance(expires_at, datetime.datetime):
        now = datetime.datetime.now()
        time_remaining = expires_at - now
        
        if time_remaining.total_seconds() <= 0:
            expires_text = "Expirado"
        else:
            # Calcular días, horas, minutos y segundos restantes
            days = time_remaining.days
            hours = time_remaining.seconds // 3600
            minutes = (time_remaining.seconds % 3600) // 60
            seconds = time_remaining.seconds % 60
            
            # Formatear texto con días, minutos y segundos
            if days > 0:
                expires_text = f"{days} días {minutes}min {seconds}s"
            elif hours > 0:
                expires_text = f"{hours}h {minutes}m {seconds}s"
            else:
                expires_text = f"{minutes} minutos {seconds} segundos"
    
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠➣ **Plan actual**: {plan_name}\n"
        f"┠➣ **Tiempo restante**:\n"
        f"┠➣ {expires_text}\n"
        f"╰✠━━━━━━━━━━━━━━━━━━✠╯"
    )

# ======================== FUNCIÓN PARA VERIFICAR VÍDEOS EN COLA ======================== #

async def has_pending_in_queue(user_id: int) -> bool:
    """Verifica si el usuario tiene videos pendientes en la cola"""
    count = pending_col.count_documents({"user_id": user_id})
    return count > 0

# ======================== FIN SISTEMA DE PLANES ======================== #

def sizeof_fmt(num, suffix="B"):
    """Formatea el tamaño de bytes a formato legible"""
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return "%3.2f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.2f%s%s" % (num, "Yi", suffix)

def create_progress_bar(current, total, proceso, length=15):
    """Crea una barra de progreso visual"""
    if total == 0:
        total = 1
    percent = current / total
    filled = int(length * percent)
    bar = '⬢' * filled + '⬡' * (length - filled)
    return (
        f'    ╭━━━[🤖**Compress Bot**]━━━╮\n'
        f'┠ [{bar}] {round(percent * 100)}%\n'
        f'┠ **Procesado**: {sizeof_fmt(current)}/{sizeof_fmt(total)}\n'
        f'┠ **Estado**: __#{proceso}__'
    )

last_progress_update = {}

async def progress_callback(current, total, msg, proceso, start_time):
    """Callback para mostrar progreso de descarga/subida - MODIFICADO: Usa compression_id"""
    try:
        # Buscar el compression_id asociado a este mensaje
        compression_id = None
        for comp_id, msg_id in active_messages.items():
            if msg_id == msg.id:
                compression_id = comp_id
                break
        
        if not compression_id:
            return
            
        # Verificar si esta compresión aún está activa
        if compression_id not in cancel_tasks:
            return
            
        now = datetime.datetime.now()
        key = (msg.chat.id, msg.id)
        last_time = last_progress_update.get(key)

        if last_time and (now - last_time).total_seconds() < 5:
            return

        last_progress_update[key] = now

        elapsed = time.time() - start_time
        percentage = current / total
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        progress_bar = create_progress_bar(current, total, proceso)
        
        # Formatear tiempos
        elapsed_str = format_time(elapsed)
        remaining_str = format_time(eta)
        
        # NUEVO: Actualizar progreso para monitoreo en tiempo real
        stage = "download" if proceso == "DESCARGA" else "upload"
        update_compression_progress(
            compression_id, 
            stage, 
            current, 
            total, 
            percentage * 100,
            "Archivo en proceso"  # Podríamos obtener el nombre del archivo si está disponible
        )
        
        # SOLO MOSTRAR BOTÓN DE CANCELACIÓN SI NO ES DESCARGA
        reply_markup = None
        if proceso != "DESCARGA":
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")
            ]])
        
        try:
            await msg.edit(
                f"   {progress_bar}\n"
                f"┠ **Velocidad** {sizeof_fmt(speed)}/s\n"
                f"┠ **Tiempo transcurrido:** {elapsed_str}\n"
                f"┠ **Tiempo restante:** {remaining_str}\n╰━━━━━━━━━━━━━━━━━━╯\n",
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editando mensaje de progreso: {e}")
            # Si falla, remover de mensajes activos
            if compression_id in active_messages:
                del active_messages[compression_id]
    except Exception as e:
        logger.error(f"Error en progress_callback: {e}", exc_info=True)

# ======================== FUNCIONALIDAD DE COLA POR ORDEN DE LLEGADA ======================== #

async def process_compression_queue():
    """Procesa la cola de compresión - MODIFICADO: Ahora múltiples workers pueden procesar simultáneamente"""
    while True:
        try:
            client, message, wait_msg = await compression_queue.get()
            
            # Verificar si la tarea aún está en pending_col (no fue cancelada)
            pending_task = pending_col.find_one({
                "chat_id": message.chat.id,
                "message_id": message.id
            })
            if not pending_task:
                logger.info(f"Tarea cancelada, saltando: {message.video.file_name}")
                compression_queue.task_done()
                continue

            start_msg = await wait_msg.edit("🗜️**Iniciando compresión**🎬")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, threading_compress_video, client, message, start_msg)
        except Exception as e:
            logger.error(f"Error procesando video: {e}", exc_info=True)
            await app.send_message(message.chat.id, f"⚠️ Error al procesar el video: {str(e)}")
        finally:
            compression_queue.task_done()

def threading_compress_video(client, message, start_msg):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(compress_video(client, message, start_msg))
    loop.close()

@app.on_message(filters.command(["deleteall"]) & filters.user(admin_users))
async def delete_all_pending(client, message):
    result = pending_col.delete_many({})
    await message.reply(f"**Cola eliminada.**\n**Se eliminaron {result.deleted_count} elementos.**")

@app.on_message(filters.regex(r"^/del_(\d+)$") & filters.user(admin_users))
async def delete_one_from_pending(client, message):
    match = message.text.strip().split("_")
    if len(match) != 2 or not match[1].isdigit():
        await message.reply("⚠️ Formato inválido. Usa `/del_1`, `/del_2`, etc.")
        return

    index = int(match[1]) - 1
    cola = list(pending_col.find().sort([("timestamp", 1)]))

    if index < 0 or index >= len(cola):
        await message.reply("⚠️ Número fuera de rango.")
        return

    eliminado = cola[index]
    pending_col.delete_one({"_id": eliminado["_id"]})

    file_name = eliminado.get("file_name", "¿?")
    user_id = eliminado["user_id"]
    tiempo = eliminado.get("timestamp")
    tiempo_str = tiempo.strftime("%Y-%m-d %H:%M:%S") if tiempo else "¿?"

    await message.reply(
        f"✅ Eliminado de la cola:\n"
        f"📁 {file_name}\n👤 ID: `{user_id}`\n⏰ {tiempo_str}"
    )

async def show_queue(client, message):
    """Muestra la cola de compresión - MODIFICADO: Usa nueva función de estado"""
    queue_status = await get_queue_status(message.from_user.id if message.from_user.id not in admin_users else None)
    await message.reply(queue_status)

@app.on_message(filters.command("auto") & filters.user(admin_users))
async def startup_command(_, message):
    """Inicia el procesamiento de la cola - MODIFICADO: Ahora inicia múltiples workers"""
    global processing_tasks
    msg = await message.reply("🔄 Iniciando procesamiento de la cola...")

    # Cargar pendientes desde la base de datos
    pendientes = pending_col.find().sort([("timestamp", 1)])
    for item in pendientes:
        try:
            user_id = item["user_id"]
            chat_id = item["chat_id"]
            message_id = item["message_id"]
            timestamp = item["timestamp"]
            
            message = await app.get_messages(chat_id, message_id)
            wait_msg = await app.send_message(chat_id, f"🔄 Recuperado desde cola persistente.")
            
            await compression_queue.put((app, message, wait_msg))
        except Exception as e:
            logger.error(f"Error cargando pendiente: {e}")

    # Crear 1 tarea de procesamiento si no existen
    if not processing_tasks or all(task.done() for task in processing_tasks):
        processing_tasks = []
        for i in range(1):  # Crear 1 workers
            task = asyncio.create_task(process_compression_queue())
            processing_tasks.append(task)
        await msg.edit("✅ Procesamiento de cola iniciado con 1 worker")
    else:
        await msg.edit("✅ Los workers de procesamiento ya están activos.")

# ======================== FIN FUNCIONALIDAD DE COLA ======================== #

def create_compression_bar(percent, bar_length=10):
    try:
        percent = max(0, min(100, percent))
        filled_length = int(bar_length * percent / 100)
        bar = '⬢' * filled_length + '⬡' * (bar_length - filled_length)
        return f"[{bar}] {int(percent)}%"
    except Exception as e:
        logger.error(f"Error creando barra de progreso: {e}", exc_info=True)
        return f"**Progreso**: {int(percent)}%"

async def compress_video(client, message: Message, start_msg):
    try:
        if not message.video:
            await app.send_message(chat_id=message.chat.id, text="Por favor envía un vídeo válido")
            return

        logger.info(f"Iniciando compresión para chat_id: {message.chat.id}, video: {message.video.file_name}")
        user_id = message.from_user.id
        original_message_id = message.id  # Guardar ID del mensaje original para cancelación

        # GENERAR ID ÚNICO PARA ESTA COMPRESIÓN
        compression_id = generate_compression_id()
        logger.info(f"Compresión ID generado: {compression_id} para usuario {user_id}")

        # Obtener configuración personalizada del usuario
        user_video_settings = await get_user_video_settings(user_id)

        # Registrar compresión activa CON COMPRESSION_ID ÚNICO
        await add_active_compression(compression_id, user_id, message.video.file_id, message.video.file_name)

        # Crear mensaje de progreso como respuesta al video original
        msg = await app.send_message(
            chat_id=message.chat.id,
            text="📥 **Iniciando Descarga** 📥",
            reply_to_message_id=message.id  # Respuesta al video original
        )
        
        # REGISTRAR MENSAJE ACTIVO CON COMPRESSION_ID
        active_messages[compression_id] = msg.id
        
        # Agregar botón de cancelación CON COMPRESSION_ID
        cancel_button = InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")
        ]])
        await msg.edit_reply_markup(cancel_button)
        
        try:
            start_download_time = time.time()
            # Registrar tarea de descarga CON COMPRESSION_ID
            register_cancelable_task(compression_id, "download", None, original_message_id=original_message_id, progress_message_id=msg.id)
            
            # NUEVO: Actualizar progreso para monitoreo
            update_compression_progress(compression_id, "download", 0, 100, 0, message.video.file_name)
            
            original_video_path = await app.download_media(
                message.video,
                progress=progress_callback,
                progress_args=(msg, "DESCARGA", start_download_time)
            )
            
            # Verificar si se canceló durante la descarga USANDO COMPRESSION_ID
            if compression_id not in cancel_tasks:
                logger.info(f"Descarga cancelada para compresión {compression_id}")
                if original_video_path and os.path.exists(original_video_path):
                    os.remove(original_video_path)
                await remove_active_compression(compression_id)
                unregister_cancelable_task(compression_id)
                remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
                # Borrar mensaje de inicio
                try:
                    await start_msg.delete()
                except:
                    pass
                # Remover de mensajes activos y borrar mensaje de progreso
                if compression_id in active_messages:
                    del active_messages[compression_id]
                try:
                    await msg.delete()
                except:
                    pass
                # Enviar mensaje de cancelación respondiendo al video original
                await send_protected_message(
                    message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
                return
                
            logger.info(f"Video descargado: {original_video_path}")
        except Exception as e:
            logger.error(f"Error en descarga: {e}", exc_info=True)
            await msg.edit(f"Error en descarga: {e}")
            await remove_active_compression(compression_id)
            unregister_cancelable_task(compression_id)
            remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
            # Remover de mensajes activos
            if compression_id in active_messages:
                del active_messages[compression_id]
            return
        
        # Verificar si se canceló después de la descarga USANDO COMPRESSION_ID
        if compression_id not in cancel_tasks:
            if original_video_path and os.path.exists(original_video_path):
                os.remove(original_video_path)
            await remove_active_compression(compression_id)
            unregister_cancelable_task(compression_id)
            remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
            # Borrar mensaje de inicio
            try:
                await start_msg.delete()
            except:
                pass
            # Remover de mensajes activos y borrar mensaje de progreso
            if compression_id in active_messages:
                del active_messages[compression_id]
            try:
                await msg.delete()
            except:
                pass
            # Enviar mensaje de cancelación respondiendo al video original
                await send_protected_message(
                    message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
            return
        
        original_size = os.path.getsize(original_video_path)
        logger.info(f"Tamaño original: {original_size} bytes")
        await notify_group(client, message, original_size, status="start")
        
        try:
            probe = ffmpeg.probe(original_video_path)
            dur_total = float(probe['format']['duration'])
            logger.info(f"Duración del video: {dur_total} segundos")
        except Exception as e:
            logger.error(f"Error obteniendo duración: {e}", exc_info=True)
            dur_total = 0

        # Mensaje de inicio de compresión como respuesta al video
        await msg.edit(
            "╭━━━━[🤖**Compress Bot**]━━━━━╮\n"
            "┠➣ 🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
            "┠➣ **Progreso**: 📤 𝘊𝘢𝘳𝘨𝘢𝘯𝘥𝘰 𝘝𝘪𝘥𝘦𝘰 📤\n"
            "╰━━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=cancel_button
        )
        
        compressed_video_path = f"{os.path.splitext(original_video_path)[0]}_compressed.mp4"
        logger.info(f"Ruta de compresión: {compressed_video_path}")
        
        drawtext_filter = f"drawtext=text='@InfiniteNetwork_KG':x=w-tw-10:y=10:fontsize=20:fontcolor=white"

        ffmpeg_command = [
            'ffmpeg', '-y', '-i', original_video_path,
            '-vf', f"scale={user_video_settings['resolution']},{drawtext_filter}",
            '-crf', user_video_settings['crf'],
            '-b:a', user_video_settings['audio_bitrate'],
            '-r', user_video_settings['fps'],
            '-preset', user_video_settings['preset'],
            '-c:v', user_video_settings['codec'],
            compressed_video_path
        ]
        logger.info(f"Comando FFmpeg: {' '.join(ffmpeg_command)}")

        try:
            start_time = datetime.datetime.now()
            process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, text=True, bufsize=1)
            
            # Registrar tarea de ffmpeg CON COMPRESSION_ID
            register_cancelable_task(compression_id, "ffmpeg", process, original_message_id=original_message_id, progress_message_id=msg.id)
            register_ffmpeg_process(compression_id, process)
            
            # NUEVO: Actualizar progreso para monitoreo
            update_compression_progress(compression_id, "compression", 0, 100, 0, message.video.file_name)
            
            last_percent = 0
            last_update_time = 0
            time_pattern = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
            
            while True:
                # Verificar si se canceló durante la compresión USANDO COMPRESSION_ID
                if compression_id not in cancel_tasks:
                    process.kill()
                    # Limpiar mensaje de progreso
                    if compression_id in active_messages:
                        del active_messages[compression_id]
                    try:
                        await msg.delete()
                        await start_msg.delete()
                    except:
                        pass
                    # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    if original_video_path and os.path.exists(original_video_path):
                        os.remove(original_video_path)
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    await remove_active_compression(compression_id)
                    unregister_cancelable_task(compression_id)
                    unregister_ffmpeg_process(compression_id)
                    remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
                    return
                
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    match = time_pattern.search(line)
                    if match and dur_total > 0:
                        time_str = match.group(1)
                        h, m, s = time_str.split(':')
                        current_time = int(h)*3600 + int(m)*60 + float(s)
                        percent = min(100, (current_time / dur_total) * 100)
                        
                        # Obtener el tamaño actual del archivo comprimido
                        compressed_size = 0
                        if os.path.exists(compressed_video_path):
                            compressed_size = os.path.getsize(compressed_video_path)
                        
                        # Calcular tiempos transcurrido y restante
                        elapsed_time = datetime.datetime.now() - start_time
                        elapsed_seconds = elapsed_time.total_seconds()
                        
                        if percent > 0:
                            remaining_seconds = (elapsed_seconds / percent) * (100 - percent)
                        else:
                            remaining_seconds = 0
                        
                        # Formatear tiempos
                        elapsed_str = format_time(elapsed_seconds)
                        remaining_str = format_time(remaining_seconds)
                        
                        # NUEVO: Actualizar progreso para monitoreo
                        update_compression_progress(compression_id, "compression", current_time, dur_total, percent, message.video.file_name)
                        
                        if percent - last_percent >= 5 or time.time() - last_update_time >= 5:
                            bar = create_compression_bar(percent)
                            # Agregar botón de cancelación CON COMPRESSION_ID
                            cancel_button = InlineKeyboardMarkup([[
                                InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_task_{compression_id}")
                            ]])
                            try:
                                await msg.edit(
                                    f"╭━━━━[**🤖Compress Bot**]━━━━━╮\n"
                                    f"┠➣ 🗜️𝗖𝗼𝗺𝗽𝗿𝗶𝗺𝗶𝗲𝗻𝗱𝗼 𝗩𝗶𝗱𝗲𝗼🎬\n"
                                    f"┠➣ **Progreso**: {bar}\n"
                                    f"┠➣ **Tamaño**: {sizeof_fmt(compressed_size)}\n"
                                    f"┠➣ **Tiempo transcurrido**: {elapsed_str}\n"
                                    f"┠➣ **Tiempo restante**: {remaining_str}\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━╯",
                                    reply_markup=cancel_button
                                )
                            except MessageNotModified:
                                pass
                            except Exception as e:
                                logger.error(f"Error editando mensaje de progreso: {e}")
                                if compression_id in active_messages:
                                    del active_messages[compression_id]
                            last_percent = percent
                            last_update_time = time.time()

            # Verificar si se canceló después de la compresión USANDO COMPRESSION_ID
            if compression_id not in cancel_tasks:
                if original_video_path and os.path.exists(original_video_path):
                    os.remove(original_video_path)
                if compressed_video_path and os.path.exists(compressed_video_path):
                    os.remove(compressed_video_path)
                await remove_active_compression(compression_id)
                unregister_cancelable_task(compression_id)
                unregister_ffmpeg_process(compression_id)
                remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
                # Borrar mensaje de inicio
                try:
                    await start_msg.delete()
                except:
                    pass
                # Remover de mensajes activos y borrar mensaje de progreso
                if compression_id in active_messages:
                    del active_messages[compression_id]
                try:
                    await msg.delete()
                except:
                    pass
                # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                return

            compressed_size = os.path.getsize(compressed_video_path)
            logger.info(f"Compresión completada. Tamaño comprimido: {compressed_size} bytes")
            
            try:
                probe = ffmpeg.probe(compressed_video_path)
                duration = int(float(probe.get('format', {}).get('duration', 0)))
                if duration == 0:
                    for stream in probe.get('streams', []):
                        if 'duration' in stream:
                            duration = int(float(stream['duration']))
                            break
                if duration == 0:
                    duration = 0
                logger.info(f"Duración del video comprimido: {duration} segundos")
            except Exception as e:
                logger.error(f"Error obteniendo duración comprimido: {e}", exc_info=True)
                duration = 0

            thumbnail_path = f"{compressed_video_path}_thumb.jpg"
            try:
                (
                    ffmpeg
                    .input(compressed_video_path, ss=duration//2 if duration > 0 else 0)
                    .filter('scale', 320, -1)
                    .output(thumbnail_path, vframes=1)
                    .overwrite_output()
                    .run(capture_stdout=True, capture_stderr=True)
                )
                logger.info(f"Miniatura generada: {thumbnail_path}")
            except Exception as e:
                logger.error(f"Error generando miniatura: {e}", exc_info=True)
                thumbnail_path = None

            processing_time = datetime.datetime.now() - start_time
            processing_time_str = str(processing_time).split('.')[0]
            

            description = (
                "╭✠━━━━━━━━━━━━━━━━━━━━✠╮\n"
                f"┠➣🗜️**Vídeo comprimído**🎬\n┠➣**Tiempo transcurrido**: {processing_time_str}\n╰✠━━━━━━━━━━━━━━━━━━━━✠╯\n"
            )
            
            try:
                start_upload_time = time.time()
                # Mensaje de subida como respuesta al video original
                upload_msg = await app.send_message(
                    chat_id=message.chat.id,
                    text="📤 **Subiendo video comprimido** 📤",
                    reply_to_message_id=message.id
                )
                # REGISTRAR MENSAJE DE SUBIDA CON COMPRESSION_ID
                active_messages[f"{compression_id}_upload"] = upload_msg.id
                
                # Registrar tarea de subida CON COMPRESSION_ID
                register_cancelable_task(compression_id, "upload", None, original_message_id=original_message_id, progress_message_id=upload_msg.id)
                
                # NUEVO: Actualizar progreso para monitoreo
                update_compression_progress(compression_id, "upload", 0, 100, 0, message.video.file_name)
                
                # Verificar si se canceló antes de la subida USANDO COMPRESSION_ID
                if compression_id not in cancel_tasks:
                    if original_video_path and os.path.exists(original_video_path):
                        os.remove(original_video_path)
                    if compressed_video_path and os.path.exists(compressed_video_path):
                        os.remove(compressed_video_path)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                    await remove_active_compression(compression_id)
                    unregister_cancelable_task(compression_id)
                    unregister_ffmpeg_process(compression_id)
                    remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
                    # Borrar mensajes
                    try:
                        await start_msg.delete()
                        await msg.delete()
                        await upload_msg.delete()
                    except:
                        pass
                    # Remover de mensajes activos
                    if compression_id in active_messages:
                        del active_messages[compression_id]
                    if f"{compression_id}_upload" in active_messages:
                        del active_messages[f"{compression_id}_upload"]
                    # Enviar mensaje de cancelación respondiendo al video original
                    await send_protected_message(
                        message.chat.id,
                        "⛔ **Compresión cancelada** ⛔",
                        reply_to_message_id=original_message_id
                    )
                    return
                
                if thumbnail_path and os.path.exists(thumbnail_path):
                    await send_protected_video(
                        chat_id=message.chat.id,
                        video=compressed_video_path,
                        caption=description,
                        thumb=thumbnail_path,
                        duration=duration,
                        reply_to_message_id=message.id,
                        progress=progress_callback,
                        progress_args=(upload_msg, "SUBIDA", start_upload_time)
                    )
                else:
                    await send_protected_video(
                        chat_id=message.chat.id,
                        video=compressed_video_path,
                        caption=description,
                        duration=duration,
                        reply_to_message_id=message.id,
                        progress=progress_callback,
                        progress_args=(upload_msg, "SUBIDA", start_upload_time)
                    )
                
                try:
                    await upload_msg.delete()
                    logger.info("Mensaje de subida eliminado")
                except:
                    pass
                logger.info("✅ Video comprimido enviado como respuesta al original")
                await notify_group(client, message, original_size, compressed_size=compressed_size, status="done")
             
                # ACTUALIZAR CONTADOR DE VIDEOS COMPRIMIDOS (CORREGIDO)
                users_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"compressed_videos": 1}},
                    upsert=True
                )
                
                try:
                    await start_msg.delete()
                    logger.info("Mensaje 'Iniciando compresión' eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de inicio: {e}")

                try:
                    await msg.delete()
                    logger.info("Mensaje de progreso eliminado")
                except Exception as e:
                    logger.error(f"Error eliminando mensaje de progreso: {e}")

            except Exception as e:
                logger.error(f"Error enviando video: {e}", exc_info=True)
                await app.send_message(chat_id=message.chat.id, text="⚠️ **Error al enviar el video comprimido**")
                
        except Exception as e:
            logger.error(f"Error en compresión: {e}", exc_info=True)
            await msg.delete()
            await app.send_message(chat_id=message.chat.id, text=f"Ocurrió un error al comprimir el video: {e}")
        finally:
            try:
                # NUEVO: ELIMINAR DE PENDING_COL CUANDO TERMINA LA COMPRESIÓN
                pending_col.delete_one({
                    "user_id": user_id,
                    "chat_id": message.chat.id,
                    "message_id": message.id
                })
                logger.info(f"Video eliminado de pending_col: {message.video.file_name}")
                
                # Limpiar mensajes activos
                if compression_id in active_messages:
                    del active_messages[compression_id]
                if f"{compression_id}_upload" in active_messages:
                    del active_messages[f"{compression_id}_upload"]
                    
                for file_path in [original_video_path, compressed_video_path]:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Archivo temporal eliminado: {file_path}")
                if 'thumbnail_path' in locals() and thumbnail_path and os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                    logger.info(f"Miniatura eliminada: {thumbnail_path}")
                    
                # NUEVO: Limpiar progreso al finalizar
                remove_compression_progress(compression_id)
                
            except Exception as e:
                logger.error(f"Error eliminando archivos temporales: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Error crítico en compress_video: {e}", exc_info=True)
        await app.send_message(chat_id=message.chat.id, text="⚠️ Ocurrió un error crítico al procesar el video")
    finally:
        await remove_active_compression(compression_id)
        unregister_cancelable_task(compression_id)
        unregister_ffmpeg_process(compression_id)
        remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso

# ======================== INTERFAZ DE USUARIO ======================== #

# Teclado principal
def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ Settings"), KeyboardButton("📋 Planes")],
            [KeyboardButton("📊 Mi Plan"), KeyboardButton("ℹ️ Ayuda")],
            [KeyboardButton("👀 Ver Cola"), KeyboardButton("🗑️ Cancelar Cola")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

@app.on_message(filters.command("settings") & filters.private)
async def settings_menu(client, message):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗜️ Compresión General", callback_data="general_menu")],
        [InlineKeyboardButton("📱 Videos en Vertical", callback_data="reels_menu")],
        [InlineKeyboardButton("📺 Shows|Calidad media", callback_data="show_menu")],
        [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime_menu")],
        [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data="custom_quality_start")]
    ])

    await send_protected_message(
        message.chat.id, 
        "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️", 
        reply_markup=keyboard
    )

# ======================== COMANDOS DE PLANES ======================== #

def get_plan_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 Estándar", callback_data="plan_standard")],
        [InlineKeyboardButton("💎 Pro", callback_data="plan_pro")],
        [InlineKeyboardButton("👑 Premium", callback_data="plan_premium")]
        # No incluir el plan ultra en el menú público
    ])

async def get_plan_menu(user_id: int):
    user = await get_user_plan(user_id)
    
    if user is None or user.get("plan") is None:
        return (
            "**No tienes un plan activo.**\n\n"
            "Adquiere un plan para usar el bot.\n\n"
            "📋 **Selecciona un plan para más información:**"
        ), get_plan_menu_keyboard()
    
    plan_name = user["plan"].capitalize()
    
    return (
        f"╭✠━━━━━━━━━━━━━━━━━━━━━━✠╮\n"
        f"┠➣ **Tu plan actual**: {plan_name}\n"
        f"╰✠━━━━━━━━━━━━━━━━━━━━━━✠╯\n\n"
        "📋 **Selecciona un plan para más información:**"
    ), get_plan_menu_keyboard()

@app.on_message(filters.command("planes") & filters.private)
async def planes_command(client, message):
    try:
        texto, keyboard = await get_plan_menu(message.from_user.id)
        await send_protected_message(
            message.chat.id, 
            texto, 
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error en planes_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ Error al mostrar los planes"
        )

# ======================== MANEJADOR DE CALLBACKS MEJORADO ======================== #
# MODIFICADO: Ahora usa compression_id único para cancelaciones

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    
    # Mapa de configuraciones para cada calidad
    config_map = {
        "general_v1": "resolution=854x480 crf=28 audio_bitrate=64k fps=22 preset=veryfast codec=libx264",
        "general_v2": "resolution=854x480 crf=28 audio_bitrate=128k fps=22 preset=veryfast codec=libx264",
        "reels_v1": "resolution=420x720 crf=25 audio_bitrate=64k fps=30 preset=veryfast codec=libx264",
        "reels_v2": "resolution=420x720 crf=25 audio_bitrate=128k fps=30 preset=veryfast codec=libx264",
        "show_v1": "resolution=854x480 crf=32 audio_bitrate=64k fps=20 preset=veryfast codec=libx264",
        "show_v2": "resolution=854x480 crf=32 audio_bitrate=128k fps=20 preset=veryfast codec=libx264",
        "anime_v1": "resolution=854x480 crf=32 audio_bitrate=64k fps=18 preset=veryfast codec=libx264",
        "anime_v2": "resolution=854x480 crf=32 audio_bitrate=128k fps=18 preset=veryfast codec=libx264"
    }

    # Nombres de calidad para mostrar
    quality_names = {
        "general_v1": "🗜️ Compresión General - V1 (audio normal)",
        "general_v2": "🗜️ Compresión General - V2 (mejor audio)",
        "reels_v1": "📱 Videos en Vertical - V1 (audio normal)",
        "reels_v2": "📱 Videos en Vertical - V2 (mejor audio)",
        "show_v1": "📺 Shows|Calidad media - V1 (audio normal)",
        "show_v2": "📺 Shows|Calidad media - V2 (mejor audio)",
        "anime_v1": "🎬 Anime y series animadas - V1 (audio normal)",
        "anime_v2": "🎬 Anime y series animadas - V2 (mejor audio)"
    }

    # ======================== NUEVO: SISTEMA DE PERSONALIZACIÓN ======================== #
    
    # Iniciar personalización de calidad
    if callback_query.data == "custom_quality_start":
        # Inicializar configuración temporal para el usuario
        temp_custom_settings[user_id] = {}
        
        # Mostrar menú de CRF
        keyboard = get_crf_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD**⚙️\n\nSelecciona el nivel de compresión CRF:",
            reply_markup=keyboard
        )
        return
    
    # Manejar selección de CRF
    elif callback_query.data.startswith("custom_crf_"):
        crf_value = callback_query.data.replace("custom_crf_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['crf'] = crf_value
        
        keyboard = get_crf_keyboard(crf_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD**⚙️\n\nSelecciona el nivel de compresión CRF:",
            reply_markup=keyboard
        )
        return
    
    # Navegar a FPS
    elif callback_query.data == "custom_next_fps":
        if user_id not in temp_custom_settings or 'crf' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un CRF primero.", show_alert=True)
            return
        
        keyboard = get_fps_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR FPS**⚙️\n\nSelecciona los frames por segundo:",
            reply_markup=keyboard
        )
        return
    
    # Volver a CRF desde FPS
    elif callback_query.data == "custom_back_crf":
        keyboard = get_crf_keyboard(temp_custom_settings.get(user_id, {}).get('crf'))
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR CALIDAD**⚙️\n\nSelecciona el nivel de compresión CRF:",
            reply_markup=keyboard
        )
        return
    
    # Manejar selección de FPS
    elif callback_query.data.startswith("custom_fps_"):
        fps_value = callback_query.data.replace("custom_fps_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['fps'] = fps_value
        
        keyboard = get_fps_keyboard(fps_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR FPS**⚙️\n\nSelecciona los frames por segundo:",
            reply_markup=keyboard
        )
        return
    
    # Navegar a Audio
    elif callback_query.data == "custom_next_audio":
        if user_id not in temp_custom_settings or 'fps' not in temp_custom_settings[user_id]:
            await callback_query.answer("Debes seleccionar un FPS primero.", show_alert=True)
            return
        
        keyboard = get_audio_keyboard()
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR AUDIO**⚙️\n\nSelecciona la calidad de audio:",
            reply_markup=keyboard
        )
        return
    
    # Volver a FPS desde Audio
    elif callback_query.data == "custom_back_fps":
        keyboard = get_fps_keyboard(temp_custom_settings.get(user_id, {}).get('fps'))
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR FPS**⚙️\n\nSelecciona los frames por segundo:",
            reply_markup=keyboard
        )
        return
    
    # Manejar selección de Audio
    elif callback_query.data.startswith("custom_audio_"):
        audio_value = callback_query.data.replace("custom_audio_", "")
        if user_id not in temp_custom_settings:
            temp_custom_settings[user_id] = {}
        temp_custom_settings[user_id]['audio_bitrate'] = audio_value
        
        keyboard = get_audio_keyboard(audio_value)
        await callback_query.message.edit_text(
            "⚙️**CONFIGURAR AUDIO**⚙️\n\nSelecciona la calidad de audio:",
            reply_markup=keyboard
        )
        return
    
    # Finalizar personalización
    elif callback_query.data == "custom_finish":
        if user_id not in temp_custom_settings:
            await callback_query.answer("Error en la configuración. Intenta nuevamente.", show_alert=True)
            return
        
        user_settings = temp_custom_settings[user_id]
        if not all(key in user_settings for key in ['crf', 'fps', 'audio_bitrate']):
            await callback_query.answer("Debes completar todos los pasos de configuración.", show_alert=True)
            return
        
        # Aplicar la configuración personalizada
        success = await apply_custom_settings(user_id, user_settings)
        
        if success:
            # Limpiar configuración temporal
            if user_id in temp_custom_settings:
                del temp_custom_settings[user_id]
            
            # Mostrar mensaje de confirmación
            confirmation_text = (
                f"✅ **CALIDAD PERSONALIZADA CONFIGURADA**\n\n"
                f"**Configuración aplicada:**\n"
                f"• **Compresión CRF:** {user_settings['crf']}\n"
                f"• **FPS:** {user_settings['fps']}\n"
                f"• **Audio:** {user_settings['audio_bitrate']}"
            )
            
            back_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver a Settings", callback_data="back_to_settings")]
            ])
            
            await callback_query.message.edit_text(
                confirmation_text,
                reply_markup=back_keyboard
            )
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
        return
    
    # ======================== FIN SISTEMA DE PERSONALIZACIÓN ========================

    # ======================== MANEJAR CANCELACIONES CON COMPRESSION_ID ÚNICO ========================
    if callback_query.data.startswith("cancel_task_"):
        compression_id = callback_query.data.split("_")[2]
        
        # Verificar que la compresión existe y pertenece al usuario
        compression_data = active_compressions_col.find_one({"compression_id": compression_id})
        if not compression_data:
            await callback_query.answer("⚠️ Esta compresión ya ha finalizado", show_alert=True)
            return
            
        if callback_query.from_user.id != compression_data["user_id"]:
            await callback_query.answer("⚠️ Solo el propietario puede cancelar esta tarea", show_alert=True)
            return
            
        if cancel_compression_task(compression_id):
            # Obtener información antes de limpiar
            task_info = cancel_tasks.get(compression_id, {})
            original_message_id = task_info.get("original_message_id")
            progress_message_id = task_info.get("progress_message_id")
            
            # Limpiar registros
            unregister_cancelable_task(compression_id)
            unregister_ffmpeg_process(compression_id)
            await remove_active_compression(compression_id)
            remove_compression_progress(compression_id)  # NUEVO: Limpiar progreso
            
            # Eliminar mensajes asociados
            msg_to_delete = callback_query.message
            try:
                await msg_to_delete.delete()
            except Exception as e:
                logger.error(f"Error eliminando mensaje de cancelación: {e}")
            
            # Remover de mensajes activos
            if compression_id in active_messages:
                del active_messages[compression_id]
            if f"{compression_id}_upload" in active_messages:
                del active_messages[f"{compression_id}_upload"]
            
            await callback_query.answer("⛔ Compresión cancelada! ⛔", show_alert=True)
            
            # Enviar mensaje de cancelación respondiendo al video original
            try:
                await app.send_message(
                    callback_query.message.chat.id,
                    "⛔ **Compresión cancelada** ⛔",
                    reply_to_message_id=original_message_id
                )
            except:
                # Si falla, enviar sin reply
                await app.send_message(
                    callback_query.message.chat.id,
                    "⛔ **Compresión cancelada** ⛔"
                )
        else:
            await callback_query.answer("⚠️ No se pudo cancelar la tarea", show_alert=True)
        return

    # ======================== RESTO DEL CÓDIGO DEL CALLBACK_HANDLER (sin cambios) ========================
    # ... (el resto del código del callback_handler se mantiene igual)
    
    # Manejar confirmaciones de compresión
    if callback_query.data.startswith(("confirm_", "cancel_")):
        action, confirmation_id_str = callback_query.data.split('_', 1)
        confirmation_id = ObjectId(confirmation_id_str)
        
        confirmation = await get_confirmation(confirmation_id)
        if not confirmation:
            await callback_query.answer("⚠️ Esta solicitud ha expirado o ya fue procesada.", show_alert=True)
            return
            
        user_id = callback_query.from_user.id
        if user_id != confirmation["user_id"]:
            await callback_query.answer("⚠️ No tienes permiso para esta acción.", show_alert=True)
            return

        if action == "confirm":
            # Verificar límite nuevamente
            if await check_user_limit(user_id):
                await callback_query.answer("⚠️ Has alcanzado tu límite mensual de compresiones.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            # Verificar si ya hay una compresión activa o en cola
            user_plan = await get_user_plan(user_id)
            queue_limit = await get_user_queue_limit(user_id)
            pending_count = pending_col.count_documents({"user_id": user_id})
            
            # Verificar límites de cola según el plan
            if pending_count >= queue_limit:
                await callback_query.answer(
                    f"⚠️ Ya tienes {pending_count} videos en cola (límite: {queue_limit}).\n"
                    "Espera a que se procesen antes de enviar más.",
                    show_alert=True
                )
                await delete_confirmation(confirmation_id)
                return

            try:
                message = await app.get_messages(confirmation["chat_id"], confirmation["message_id"])
            except Exception as e:
                logger.error(f"Error obteniendo mensaje: {e}")
                await callback_query.answer("⚠️ Error al obtener el video. Intenta enviarlo de nuevo.", show_alert=True)
                await delete_confirmation(confirmation_id)
                return

            await delete_confirmation(confirmation_id)
            
            # Editar mensaje de confirmación para mostrar estado
            queue_size = compression_queue.qsize()
            wait_msg = await callback_query.message.edit_text(
                f"✅ Tu video ha sido añadido a la cola.\n\n"
                f"• ⏳**Espere que otros procesos terminen** ⏳"
            )

            # Obtener timestamp y encolar
            timestamp = datetime.datetime.now()
            
            # Crear tarea de procesamiento si no existen
            global processing_tasks
            if not processing_tasks or all(task.done() for task in processing_tasks):
                processing_tasks = []
                for i in range(1):  # Crear 1 workers
                    task = asyncio.create_task(process_compression_queue())
                    processing_tasks.append(task)
            
            # Insertar en pending_col incluyendo el wait_message_id
            pending_col.insert_one({
                "user_id": user_id,
                "video_id": message.video.file_id,
                "file_name": message.video.file_name,
                "chat_id": message.chat.id,
                "message_id": message.id,
                "wait_message_id": wait_msg.id,  # <--- Nuevo campo
                "timestamp": timestamp
            })
            
            await compression_queue.put((app, message, wait_msg))
            logger.info(f"Video confirmado y encolado de {user_id}: {message.video.file_name}")

        elif action == "cancel":
            await delete_confirmation(confirmation_id)
            await callback_query.answer("⛔ Compresión cancelada.⛔", show_alert=True)
            try:
                await callback_query.message.edit_text("⛔ **Compresión cancelada.** ⛔")
                # Borrar mensaje después de 5 segundos
                await asyncio.sleep(5)
                await callback_query.message.delete()
            except:
                pass
        return

    # Manejar menús de calidades
    if callback_query.data.endswith("_menu"):
        quality_type = callback_query.data.replace("_menu", "")
        
        if quality_type == "general":
            title = "🗜️ Compresión General"
        elif quality_type == "reels":
            title = "📱 Videos en Vertical"
        elif quality_type == "show":
            title = "📺 Shows|Calidad media"
        elif quality_type == "anime":
            title = "🎬 Anime y series animadas"
        else:
            title = "Seleccionar Calidad"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("V1 (audio normal)", callback_data=f"{quality_type}_v1")],
            [InlineKeyboardButton("V2 (mejor audio)", callback_data=f"{quality_type}_v2")],
            [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
        ])
        
        await callback_query.message.edit_text(
            f"{title}\n\nSelecciona la calidad de audio:",
            reply_markup=keyboard
        )
        return

    # Resto de callbacks (planes, configuraciones, etc.)
    if callback_query.data == "plan_back":
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error en plan_back: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al volver al menú de planes", show_alert=True)
        return

    # Manejar el callback para mostrar planes desde el mensaje de start o video
    if callback_query.data in ["show_plans_from_start", "show_plans_from_video"]:
        try:
            texto, keyboard = await get_plan_menu(callback_query.from_user.id)
            await callback_query.message.edit_text(texto, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error mostrando planes desde callback: {e}", exc_info=True)
            await callback_query.answer("⚠️ Error al mostrar los planes", show_alert=True)
        return

    # Manejar callbacks de planes
    elif callback_query.data.startswith("plan_"):
        plan_type = callback_query.data.split("_")[1]
        user_id = callback_query.from_user.id
        
        # Nuevo teclado con botón de contratar
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Volver", callback_data="plan_back"),
             InlineKeyboardButton("📝 Contratar Plan", url="https://t.me/InfiniteNetworkAdmin?text=Hola,+estoy+interesad@+en+un+plan+del+bot+de+comprimír+vídeos")]
        ])
        
        if plan_type == "standard":
            await callback_query.message.edit_text(
                "🧩**Plan Estándar**🧩\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n\n"
                "❌ **Desventajas:**\n• **No podá reenviar del bot**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **180Cup**💳 | **100Cup**📱\n• **Duración 7 dias**\n\n",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "pro":
            await callback_query.message.edit_text(
                "💎**Plan Pro**💎\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Podá reenviar del bot**\n\n❌ **Desventajas**\n• **Solo podá comprimír 1 video a la ves**\n\n• **Precio:** **300Cup**💳 | **200Cup**📱\n• **Duración 15 dias**\n\n",
                reply_markup=back_keyboard
            )
            
        elif plan_type == "premium":
            await callback_query.message.edit_text(
                "👑**Plan Premium**👑\n\n"
                "✅ **Beneficios:**\n"
                "• **Videos para comprimir: ilimitados**\n"
                "• **Soporte prioritario 24/7**\n• **Podá reenviar del bot**\n"
                f"• **Múltiples videos en cola** (hasta {PREMIUM_QUEUE_LIMIT})\n\n"
                "• **Precio:** **500Cup**💳 | **300Cup**📱\n• **Duración 30 dias**\n\n",
                reply_markup=back_keyboard
            )
        return

    # Manejar configuraciones de calidad
    config = config_map.get(callback_query.data)
    if config:
        # Actualizar configuración personalizada del usuario
        user_id = callback_query.from_user.id
        if await update_user_video_settings(user_id, config):
            back_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Volver", callback_data="back_to_settings")]
            ])
            
            quality_name = quality_names.get(callback_query.data, "Calidad Desconocida")
            
            # Mostrar mensaje de confirmación específico según la calidad seleccionada
            if callback_query.data.endswith("_v1"):
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            elif callback_query.data.endswith("_v2"):
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            else:
                message_text = f"**{quality_name}\naplicada correctamente**✅"
            
            await callback_query.message.edit_text(
                message_text,
                reply_markup=back_keyboard
            )
        else:
            await callback_query.answer("❌ Error al aplicar la configuración", show_alert=True)
    elif callback_query.data == "back_to_settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗜️ Compresión General", callback_data="general_menu")],
            [InlineKeyboardButton("📱 Videos en Vertical", callback_data="reels_menu")],
            [InlineKeyboardButton("📺 Shows|Calidad media", callback_data="show_menu")],
            [InlineKeyboardButton("🎬 Anime y series animadas", callback_data="anime_menu")],
            [InlineKeyboardButton("🛠️ Personalizar Calidad 🔧", callback_data="custom_quality_start")]
        ])
        await callback_query.message.edit_text(
            "⚙️𝗦𝗲𝗹𝗲𝗰𝗰𝗶𝗼𝗻𝗮𝗿 𝗖𝗮𝗹𝗶𝗱𝗮𝗱⚙️",
            reply_markup=keyboard
        )
    else:
        await callback_query.answer("Opción inválida.", show_alert=True)

# ======================== MANEJADOR DE START CON MENÚ ======================== #

@app.on_message(filters.command("start"))
async def start_command(client, message):
    try:
        user_id = message.from_user.id
        
        # Verificar si el usuario está baneado
        if user_id in ban_users:
            logger.warning(f"Usuario baneado intentó usar /start: {user_id}")
            return

        # Verificar si el usuario tiene un plan (está registrado)
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            # Usuario sin plan: mostrar mensaje de acceso denegado con botón de ofertas
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso al bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
            return

        # Usuario con plan: mostrar menú normal
        # Ruta de la imagen del logo
        image_path = "logo.jpg"
        
        caption = (
            "**🤖 Bot para comprimir videos**\n"
            "➣**Creado por** @InfiniteNetworkAdmin\n\n"
            "**¡Bienvenido!** Puedo reducir el tamaño de los vídeos hasta un 80% o más y se verán bien sin perder tanta calidad\nUsa los botones del menú para interactuar conmigo.\nSi tiene duda use el botón ℹ️ Ayuda\n\n"
            "**⚙️ Versión 21.5.0 ⚙️**"
        )
        
        # Enviar la foto con el caption
        await send_protected_photo(
            chat_id=message.chat.id,
            photo=image_path,
            caption=caption,
            reply_markup=get_main_menu_keyboard()
        )
        logger.info(f"Comando /start ejecutado por {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en handle_start: {e}", exc_info=True)

# ======================== MANEJADOR DE MENÚ PRINCIPAL ======================== #

@app.on_message(filters.text & filters.private)
async def main_menu_handler(client, message):
    try:
        text = message.text.lower()
        user_id = message.from_user.id

        if user_id in ban_users:
            return
            
        if text == "⚙️ settings":
            await settings_menu(client, message)
        elif text == "📋 planes":
            await planes_command(client, message)
        elif text == "📊 mi plan":
            await my_plan_command(client, message)
        elif text == "ℹ️ ayuda":
            # Crear teclado con botón de soporte
            support_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("👨🏻‍💻 Soporte", url="https://t.me/InfiniteNetworkAdmin")]
            ])
            
            await send_protected_message(
                message.chat.id,
                "👨🏻‍💻 **Información**\n\n"
                "➣ **Configurar calidad**:\n• Usa el botón ⚙️ Settings\n"
                "➣ **Para comprimir un video**:\n• Envíalo directamente al bot\n"
                "➣ **Ver planes**:\n• Usa el botón 📋 Planes\n"
                "➣ **Ver tu estado**:\n• Usa el botón 📊 Mi Plan\n"
                "➣ **Usa** /start **para iniciar en el bot nuevamente o para actualizar**\n"
                "➣ **Ver cola de compresión**:\n• Usa el botón 👀 Ver Cola\n"
                "➣ **Cancelar videos de la cola**:\n• Usa el botón 🗑️ Cancelar Cola\n➣ **Para ver su configuración de compresión actual use** /calidad\n\n",
                reply_markup=support_keyboard
            )
        elif text == "👀 ver cola":
            await queue_command(client, message)
        elif text == "🗑️ cancelar cola":
            await cancel_queue_command(client, message)
        elif text == "/cancel":
            await cancel_command(client, message)
        else:
            # Manejar otros comandos de texto existentes
            await handle_message(client, message)
            
    except Exception as e:
        logger.error(f"Error en main_menu_handler: {e}", exc_info=True)

# ======================== NUEVO COMANDO PARA DESBANEAR USUARIOS ======================== #

@app.on_message(filters.command("desuser") & filters.user(admin_users))
async def unban_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /desuser <user_id>")
            return

        user_id = int(parts[1])
        
        if user_id in ban_users:
            ban_users.remove(user_id)
            
        result = banned_col.delete_one({"user_id": user_id})
        
        if result.deleted_count > 0:
            await message.reply(f"Usuario {user_id} desbaneado exitosamente.")
            # Notificar al usuario que fue desbaneado
            try:
                await app.send_message(
                    user_id,
                    "✅ **Tu acceso al bot ha sido restaurado.**\n\n"
                    "Ahora puedes volver a usar el bot."
                )
            except Exception as e:
                logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
        else:
            await message.reply(f"El usuario {user_id} no estaba baneado.")
            
        logger.info(f"Usuario desbaneado: {user_id} por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en unban_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al desbanear usuario. Formato: /desuser [user_id]")

# ======================== NUEVO COMANDO DELETEUSER ======================== #

@app.on_message(filters.command("deleteuser") & filters.user(admin_users))
async def delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /deleteuser <user_id>")
            return

        user_id = int(parts[1])
        
        # Eliminar usuario de la base de datos
        result = users_col.delete_one({"user_id": user_id})
        
        # Agregar a lista de baneados si no está
        if user_id not in ban_users:
            ban_users.append(user_id)
            
        # Agregar a colección de baneados
        banned_col.insert_one({
            "user_id": user_id,
            "banned_at": datetime.datetime.now()
        })
        
        # Eliminar tareas pendientes del usuario
        pending_result = pending_col.delete_many({"user_id": user_id})
        
        # Eliminar configuración personalizada del usuario
        user_settings_col.delete_one({"user_id": user_id})
        
        await message.reply(
            f"Usuario {user_id} eliminado y baneado exitosamente.\n"
            f"🗑️ Tareas pendientes eliminadas: {pending_result.deleted_count}"
        )
        
        logger.info(f"Usuario eliminado y baneado: {user_id} por admin {message.from_user.id}")
        
        # Notificar al usuario que perdió el acceso
        try:
            await app.send_message(
                user_id,
                "🔒 **Tu acceso al bot ha sido revocado.**\n\n"
                "No podrás usar el bot hasta nuevo aviso."
            )
        except Exception as e:
            logger.error(f"No se pudo notificar al usuario {user_id}: {e}")
            
    except Exception as e:
        logger.error(f"Error en delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuario. Formato: /deleteuser [user_id]")

# ======================== NUEVO COMANDO PARA VER USUARIOS BANEADOS ======================== #

@app.on_message(filters.command("viewban") & filters.user(admin_users))
async def view_banned_users_command(client, message):
    try:
        banned_users = list(banned_col.find({}))
        
        if not banned_users:
            await message.reply("**No hay usuarios baneados.**")
            return

        response = "**Usuarios Baneados**\n\n"
        for i, banned_user in enumerate(banned_users, 1):
            user_id = banned_user["user_id"]
            banned_at = banned_user.get("banned_at", "Fecha desconocida")
            
            # Obtener información del usuario de Telegram
            try:
                user = await app.get_users(user_id)
                username = f"@{user.username}" if user.username else "Sin username"
            except:
                username = "Sin username"
            
            if isinstance(banned_at, datetime.datetime):
                banned_at_str = banned_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                banned_at_str = str(banned_at)
                
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   ⏰ Fecha: {banned_at_str}\n\n"

        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en view_banned_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al obtener la lista de usuarios baneados")

# ======================== COMANDO PARA ELIMINAR USUARIOS ======================== #
@app.on_message(filters.command(["banuser", "deluser"]) & filters.user(admin_users))
async def ban_or_delete_user_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /comando <user_id>")
            return

        ban_user_id = int(parts[1])

        if ban_user_id in admin_users:
            await message.reply("No puedes banear a un administrador.")
            return

        result = users_col.delete_one({"user_id": ban_user_id})

        if ban_user_id not in ban_users:
            ban_users.append(ban_user_id)
            
        banned_col.insert_one({
            "user_id": ban_user_id,
            "banned_at": datetime.datetime.now()
        })

        # Eliminar configuración personalizada del usuario
        user_settings_col.delete_one({"user_id": ban_user_id})

        await message.reply(
            f"Usuario {ban_user_id} baneado y eliminado de la base de datos."
            if result.deleted_count > 0 else
            f"Usuario {ban_user_id} baneado (no estaba en la base de datos)."
        )
    except Exception as e:
        logger.error(f"Error en ban_or_delete_user_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

@app.on_message(filters.command("key") & filters.private)
async def key_command(client, message):
    try:
        user_id = message.from_user.id
        
        if user_id in ban_users:
            await send_protected_message(message.chat.id, "🚫 Tu acceso ha sido revocado.")
            return
            
        logger.info(f"Comando key recibido de {user_id}")
        
        # Obtener la clave directamente del texto del mensaje
        if not message.text or len(message.text.split()) < 2:
            await send_protected_message(message.chat.id, "❌ Formato: /key <clave>")
            return

        key = message.text.split()[1].strip()  # Obtener la clave directamente del texto

        now = datetime.datetime.now()
        key_data = temp_keys_col.find_one({
        "key": key,
        "used": False
    })

        if not key_data:
            await send_protected_message(message.chat.id, "❌ **Clave inválida o ya ha sido utilizada.**")
            return

        # Verificar si la clave ha expirado
        if key_data["expires_at"] < now:
            await send_protected_message(message.chat.id, "❌ **La clave ha expirado.**")
            return

        # Si llegamos aquí, la clave es válida
        temp_keys_col.update_one({"_id": key_data["_id"]}, {"$set": {"used": True}})
        new_plan = key_data["plan"]
        
        # Calcular fecha de expiración usando los nuevos campos
        duration_value = key_data["duration_value"]
        duration_unit = key_data["duration_unit"]
        
        if duration_unit == "minutes":
            expires_at = datetime.datetime.now() + datetime.timedelta(minutes=duration_value)
        elif duration_unit == "hours":
            expires_at = datetime.datetime.now() + datetime.timedelta(hours=duration_value)
        else:  # días por defecto
            expires_at = datetime.datetime.now() + datetime.timedelta(days=duration_value)
            
        success = await set_user_plan(user_id, new_plan, notify=False, expires_at=expires_at)
        
        if success:
            # Texto para mostrar la duración en formato amigable
            duration_text = f"{duration_value} {duration_unit}"
            if duration_value == 1:
                duration_text = duration_text[:-1]  # Remover la 's' final para singular
            
            await send_protected_message(
                message.chat.id,
                f"✅ **Plan {new_plan.capitalize()} activado!**\n"
                f"**Válido por {duration_text}**\n\n"
                f"Use el comando /start para iniciar en el bot"
            )
            logger.info(f"Plan actualizado a {new_plan} para {user_id} con clave {key}")
        else:
            await send_protected_message(message.chat.id, "❌ **Error al activar el plan. Contacta con el administrador.**")

    except Exception as e:
        logger.error(f"Error en key_command: {e}", exc_info=True)
        await send_protected_message(message.chat.id, "❌ **Error al procesar la solicitud de acceso**")

sent_messages = {}

def is_bot_public():
    return BOT_IS_PUBLIC and BOT_IS_PUBLIC.lower() == "true"

# ======================== COMANDOS DE PLANES ======================== #

@app.on_message(filters.command("myplan") & filters.private)
async def my_plan_command(client, message):
    try:
        user_id = message.from_user.id
        user_plan = await get_user_plan(user_id)
        
        if user_plan is None or user_plan.get("plan") is None:
            # Mostrar mensaje con botón de planes
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠 Planes 💠", callback_data="show_plans_from_start")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n⬇️**Toque para ver nuestros planes**⬇️",
                reply_markup=keyboard
            )
        else:
            plan_info = await get_plan_info(user_id)
            await send_protected_message(
                message.chat.id, 
                plan_info,
                reply_markup=get_main_menu_keyboard()
            )
    except Exception as e:
        logger.error(f"Error en my_plan_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id, 
            "⚠️ **Error al obtener información de tu plan**",
            reply_markup=get_main_menu_keyboard()
        )

@app.on_message(filters.command("setplan") & filters.user(admin_users))
async def set_plan_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("Formato: /setplan <user_id> <plan>")
            return
        
        user_id = int(parts[1])
        plan = parts[2].lower()
        
        if plan not in PLAN_DURATIONS:
            await message.reply(f"⚠️ Plan inválido. Opciones válidas: {', '.join(PLAN_DURATIONS.keys())}")
            return
        
        # Usar set_user_plan sin expires_at para que calcule automáticamente
        if await set_user_plan(user_id, plan):
            await message.reply(f"**Plan del usuario {user_id} actualizado a {plan}.**")
        else:
            await message.reply("⚠️ **Error al actualizar el plan.**")
    except Exception as e:
        logger.error(f"Error en set_plan_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error en el comando**")

@app.on_message(filters.command("userinfo") & filters.user(admin_users))
async def user_info_command(client, message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("Formato: /userinfo <user_id>")
            return
        
        user_id = int(parts[1])
        user = await get_user_plan(user_id)
        
        # Obtener información del usuario de Telegram
        try:
            user_info = await app.get_users(user_id)
            username = f"@{user_info.username}" if user_info.username else "Sin username"
        except:
            username = "Sin username"
            
        if user:
            plan_name = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            join_date = user.get("join_date", "Desconocido")
            expires_at = user.get("expires_at", "No expira")
            compressed_videos = user.get("compressed_videos", 0)  # Nuevo campo

            if isinstance(join_date, datetime.datetime):
                join_date = join_date.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(expires_at, datetime.datetime):
                expires_at = expires_at.strftime("%Y-%m-%d %H:%M:%S")

            await message.reply(
                f"👤**Usuario**: {username}\n"
                f"🆔 **ID**: `{user_id}`\n"
                f"📝 **Plan**: {plan_name}\n"
                f"🎬 **Videos comprimidos**: {compressed_videos}\n"
                f"📅 **Fecha de registro**: {join_date}\n"
                f"⏰ **Expira**: {expires_at}"
            )
        else:
            await message.reply("⚠️ Usuario no registrado o sin plan")
    except Exception as e:
        logger.error(f"Error en user_info_command: {e}", exc_info=True)
        await message.reply("⚠️ Error en el comando")

# ======================== NUEVO COMANDO RESTUSER ======================== #

@app.on_message(filters.command("restuser") & filters.user(admin_users))
async def reset_all_users_command(client, message):
    try:
        result = users_col.delete_many({})
        
        # También eliminar todas las configuraciones personalizadas
        user_settings_col.delete_many({})
        
        await message.reply(
            f"**Todos los usuarios han sido eliminados**\n"
            f"Usuarios eliminados: {result.deleted_count}"
        )
        logger.info(f"Todos los usuarios eliminados por admin {message.from_user.id}")
    except Exception as e:
        logger.error(f"Error en reset_all_users_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al eliminar usuarios")

# ======================== NUEVOS COMANDOS DE ADMINISTRACIÓN ======================== #

@app.on_message(filters.command("user") & filters.user(admin_users))
async def list_users_command(client, message):
    try:
        all_users = list(users_col.find({}))
        
        if not all_users:
            await message.reply("⛔**No hay usuarios registrados.**⛔")
            return

        response = "**Lista de Usuarios Registrados**\n\n"
        for i, user in enumerate(all_users, 1):
            user_id = user["user_id"]
            plan = user["plan"].capitalize() if user.get("plan") else "Ninguno"
            
            try:
                user_info = await app.get_users(user_id)
                username = f"@{user_info.username}" if user_info.username else "Sin username"
            except:
                username = "Sin username"
                
            response += f"{i}• 👤 {username}\n   🆔 ID: `{user_id}`\n   📝 Plan: {plan}\n\n"

        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en list_users_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al listar usuarios**")

@app.on_message(filters.command("admin") & filters.user(admin_users))
async def admin_stats_command(client, message):
    try:
        pipeline = [
            {"$match": {"plan": {"$exists": True, "$ne": None}}},
            {"$group": {
                "_id": "$plan",
                "count": {"$sum": 1}
            }}
        ]
        stats = list(users_col.aggregate(pipeline))
        
        total_users = users_col.count_documents({})
        
        response = "📊 **Estadísticas de Administrador**\n\n"
        response += f"👥 **Total de usuarios:** {total_users}\n\n"
        response += "📝 **Distribución por Planes:**\n"
        
        plan_names = {
            "standard": "🧩 Estándar",
            "pro": "💎 Pro",
            "premium": "👑 Premium",
            "ultra": "🚀 Ultra"
        }
        
        for stat in stats:
            plan_type = stat["_id"]
            count = stat["count"]
            plan_name = plan_names.get(
                plan_type, 
                plan_type.capitalize() if plan_type else "❓ Desconocido"
            )
            
            response += (
                f"\n{plan_name}:\n"
                f"  👥 Usuarios: {count}\n"
            )
        
        await message.reply(response)
    except Exception as e:
        logger.error(f"Error en admin_stats_command: {e}", exc_info=True)
        await message.reply("⚠️ **Error al generar estadísticas**")

# ======================== NUEVO COMANDO BROADCAST ======================== #

async def broadcast_message(admin_id: int, message_text: str):
    try:
        user_ids = set()
        
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        
        if total_users == 0:
            await app.send_message(admin_id, "📭 No hay usuarios para enviar el mensaje.")
            return
        
        await app.send_message(
            admin_id,
            f"📤 **Iniciando difusión a {total_users} usuarios...**\n"
            f"⏱ Esto puede tomar varios minutos."
        )
        
        success = 0
        failed = 0
        count = 0
        
        for user_id in user_ids:
            count += 1
            try:
                await send_protected_message(user_id, f"**🔔Notificación:**\n\n{message_text}")
                success += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error enviando mensaje a {user_id}: {e}")
                failed += 1
                    
        await app.send_message(
            admin_id,
            f"✅ **Difusión completada!**\n\n"
            f"👥 Total de usuarios: {total_users}\n"
            f"✅ Enviados correctamente: {success}\n"
            f"❌ Fallidos: {failed}"
        )
    except Exception as e:
        logger.error(f"Error en broadcast_message: {e}", exc_info=True)
        await app.send_message(admin_id, f"⚠️ Error en difusión: {str(e)}")

@app.on_message(filters.command("msg") & filters.user(admin_users))
async def broadcast_command(client, message):
    try:
        # Verificar si el mensaje tiene texto
        if not message.text or len(message.text.split()) < 2:
            await message.reply("⚠️ Formato: /msg <mensaje>")
            return
            
        # Obtener el texto después del comando
        parts = message.text.split(maxsplit=1)
        broadcast_text = parts[1] if len(parts) > 1 else ""
        
        # Validar que haya texto para difundir
        if not broadcast_text.strip():
            await message.reply("⚠️ El mensaje no puede estar vacío")
            return
            
        admin_id = message.from_user.id
        asyncio.create_task(broadcast_message(admin_id, broadcast_text))
        
        await message.reply(
            "📤 **Difusión iniciada!**\n"
            "⏱ Los mensajes se enviarán progresivamente a todos los usuarios.\n"
            "Recibirás un reporte final cuando se complete."
        )
    except Exception as e:
        logger.error(f"Error en broadcast_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al iniciar la difusión")

# ======================== NUEVO COMANDO PARA VER COLA ======================== #

async def queue_command(client, message):
    """Muestra información sobre la cola de compresión - MODIFICADO: Usa misma vista para todos"""
    user_id = message.from_user.id
    user_plan = await get_user_plan(user_id)
    
    if user_plan is None or user_plan.get("plan") is None:
        await send_protected_message(
            message.chat.id,
            "**Usted no tiene acceso para usar este bot.**\n\n"
            "Por favor, adquiera un plan para poder ver la cola de compresión."
        )
        return
    
    # Usar la misma función get_queue_status para todos
    queue_status = await get_queue_status(user_id)
    await send_protected_message(message.chat.id, queue_status)

# ======================== NUEVA FUNCIÓN PARA NOTIFICAR A TODOS LOS USUARIOS ======================== #

async def notify_all_users(message_text: str):
    """Envía un mensaje a todos los usuarios registrados y no baneados"""
    try:
        user_ids = set()
        
        # Obtener todos los usuarios registrados (que tienen un plan)
        for user in users_col.find({}, {"user_id": 1}):
            user_ids.add(user["user_id"])
        
        # Filtrar usuarios baneados
        user_ids = [uid for uid in user_ids if uid not in ban_users]
        total_users = len(user_ids)
        
        if total_users == 0:
            return 0, 0
        
        success = 0
        failed = 0
        
        for user_id in user_ids:
            try:
                await send_protected_message(user_id, message_text)
                success += 1
                # Pequeña pausa para no saturar
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error enviando mensaje de notificación a {user_id}: {e}")
                failed += 1
                    
        return success, failed
    except Exception as e:
        logger.error(f"Error en notify_all_users: {e}", exc_info=True)
        return 0, 0

# ======================== NUEVO COMANDO RESTART ======================== #

async def restart_bot():
    """Función para reiniciar el bot cancelando todos los procesos"""
    try:
        # 1. Cancelar todos los procesos FFmpeg activos
        for user_id, process in list(ffmpeg_processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
            except Exception as e:
                logger.error(f"Error terminando proceso FFmpeg para {user_id}: {e}")
        
        # 2. Limpiar estructuras de datos de procesos
        ffmpeg_processes.clear()
        cancel_tasks.clear()
        
        # 3. Limpiar mensajes activos
        active_messages.clear()
        
        # 4. Limpiar la cola de compresión
        while not compression_queue.empty():
            try:
                compression_queue.get_nowait()
                compression_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        # 5. Eliminar todos los pendientes de la base de datos
        result = pending_col.delete_many({})
        logger.info(f"Eliminados {result.deleted_count} elementos de la cola")
        
        # 6. Limpiar compresiones activas
        active_compressions_col.delete_many({})
        
        # 7. Notificar a todos los usuarios
        notification_text = (
            "🔔**Notificación:**\n\n"
            "El bot ha sido reiniciado\ntodos los procesos se han cancelado.\n\n✅ **Ahora puedes enviar nuevos videos para comprimir**."
        )
        
        # Enviar notificación a todos los usuarios en segundo plano
        success, failed = await notify_all_users(notification_text)
        
        # 8. Notificar al grupo de administradores
        try:
            await app.send_message(
                -4826894501,  # Reemplaza con tu ID de grupo
                f"**Notificación de reinicio completada!**\n\n"
                f"✅ Enviados correctamente: {success}\n"
                f"❌ Fallidos: {failed}"
            )
        except Exception as e:
            logger.error(f"Error enviando notificación de reinicio al grupo: {e}")
        
        return True, success, failed
    except Exception as e:
        logger.error(f"Error en restart_bot: {e}", exc_info=True)
        return False, 0, 0

@app.on_message(filters.command("restart") & filters.user(admin_users))
async def restart_command(client, message):
    """Comando para reiniciar el bot y cancelar todos los procesos"""
    try:
        msg = await message.reply("🔄 Reiniciando bot...")
        
        success, notifications_sent, notifications_failed = await restart_bot()
        
        if success:
            await msg.edit(
                "**Bot reiniciado con éxito**\n\n"
                "✅ Todos los procesos activos cancelados\n"
                "✅ Cola de compresión vaciada\n"
                "✅ Procesos FFmpeg terminados\n"
                "✅ Estado interno limpiado\n\n"
                f"📤 Notificaciones enviadas: {notifications_sent}\n"
                f"❌ Notificaciones fallidas: {notifications_failed}"
            )
        else:
            await msg.edit("⚠️ **Error al reiniciar el bot.**")
    except Exception as e:
        logger.error(f"Error en restart_command: {e}", exc_info=True)
        await message.reply("⚠️ Error al ejecutar el comando de reinicio")

# ======================== NUEVOS COMANDOS PARA CONFIGURACIÓN PERSONALIZADA ======================== #

@app.on_message(filters.command(["calidad", "quality"]) & filters.private)
async def calidad_command(client, message):
    """Permite a los usuarios establecer su configuración personalizada de compresión"""
    try:
        user_id = message.from_user.id
        
        # Verificar si el usuario tiene un plan activo
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            await send_protected_message(
                message.chat.id,
                "**Usted no tiene acceso para usar este bot.**\n\n⬇️**Toque para ver nuestros planes**⬇️"
            )
            return
            
        # Verificar si se proporcionaron parámetros
        if len(message.text.split()) < 2:
            # Mostrar la configuración actual del usuario
            current_settings = await get_user_video_settings(user_id)
            response = (
                "**Tu configuración actual de compresión:**\n\n"
                f"• **Resolución**: `{current_settings['resolution']}`\n"
                f"• **CRF**: `{current_settings['crf']}`\n"
                f"• **Bitrate de audio**: `{current_settings['audio_bitrate']}`\n"
                f"• **FPS**: `{current_settings['fps']}`\n"
                f"• **Preset**: `{current_settings['preset']}`\n"
                f"• **Códec**: `{current_settings['codec']}`\n\n"
                "Para restablecer a la configuración por defecto, usa /resetcalidad"
            )
            await send_protected_message(message.chat.id, response)
            return
            
        # Procesar la nueva configuración
        command_text = message.text.split(maxsplit=1)[1]
        success = await update_user_video_settings(user_id, command_text)
        
        if success:
            new_settings = await get_user_video_settings(user_id)
            response = "✅ **Configuración actualizada correctamente:**\n\n"
            for key, value in new_settings.items():
                response += f"• **{key}**: `{value}`\n"
                
            await send_protected_message(message.chat.id, response)
        else:
            await send_protected_message(
                message.chat.id,
                "❌ **Error al actualizar la configuración.**\n"
                "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
            )
            
    except Exception as e:
        logger.error(f"Error en calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al procesar el comando.**\n"
            "Formato correcto: /calidad resolution=854x480 crf=28 audio_bitrate=64k fps=25 preset=veryfast codec=libx264"
        )

@app.on_message(filters.command("resetcalidad") & filters.private)
async def reset_calidad_command(client, message):
    """Restablece la configuración del usuario a los valores por defecto"""
    try:
        user_id = message.from_user.id
        await reset_user_video_settings(user_id)
        
        default_settings = await get_user_video_settings(user_id)
        response = "✅ **Configuración restablecida a los valores por defecto:**\n\n"
        for key, value in default_settings.items():
            response += f"• **{key}**: `{value}`\n"
            
        await send_protected_message(message.chat.id, response)
        
    except Exception as e:
        logger.error(f"Error en reset_calidad_command: {e}", exc_info=True)
        await send_protected_message(
            message.chat.id,
            "❌ **Error al restablecer la configuración.**"
        )

# ======================== MANEJADORES PRINCIPALES ======================== #

# Manejador para vídeos recibidos
@app.on_message(filters.video)
async def handle_video(client, message: Message):
    try:
        user_id = message.from_user.id
        
        # Paso 1: Verificar baneo
        if user_id in ban_users:
            logger.warning(f"Intento de uso por usuario baneado: {user_id}")
            return
        
        # Paso 2: Verificar si el usuario tiene un plan
        user_plan = await get_user_plan(user_id)
        if user_plan is None or user_plan.get("plan") is None:
            # Mostrar mensaje con botón de ofertas
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💠Planes💠", callback_data="show_plans_from_video")]
            ])
            await send_protected_message(
                message.chat.id,
                "**No tienes un plan activo.**\n\n"
                "**Adquiere un plan para usar el bot.**\n\n",
                reply_markup=keyboard
            )
            return
        
        # Paso 3: Verificar si ya tiene una confirmación pendiente
        if await has_pending_confirmation(user_id):
            logger.info(f"Usuario {user_id} tiene confirmación pendiente, ignorando video adicional")
            return
        
        # Paso 4: Verificar límite de plan
        if await check_user_limit(user_id):
            await send_protected_message(
                message.chat.id,
                f"⚠️ **Límite alcanzado**\n"
                f"Tu plan ha expirado.\n\n"
                "👨🏻‍💻**Contacta con @InfiniteNetworkAdmin para renovar tu Plan**"
            )
            return
        
        # Paso 5: Verificar si el usuario puede agregar más vídeos a la cola
        has_active = await has_active_compression(user_id)
        queue_limit = await get_user_queue_limit(user_id)
        pending_count = pending_col.count_documents({"user_id": user_id})

        # Verificar límites de cola según el plan
        if pending_count >= queue_limit:
            await send_protected_message(
                message.chat.id,
                f"Ya tienes {pending_count} videos en cola (límite: {queue_limit}).\n"
                "Por favor espera a que se procesen antes de enviar más."
            )
            return
        
        # Paso 6: Crear confirmación pendiente
        confirmation_id = await create_confirmation(
            user_id,
            message.chat.id,
            message.id,
            message.video.file_id,
            message.video.file_name
        )
        
        # Paso 7: Enviar mensaje de confirmación con botones (respondiendo al video)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Confirmar compresión 🟢", callback_data=f"confirm_{confirmation_id}")],
            [InlineKeyboardButton("⛔ Cancelar ⛔", callback_data=f"cancel_{confirmation_id}")]
        ])
        
        await send_protected_message(
            message.chat.id,
            f"🎬 **Video recibido para comprimír:** `{message.video.file_name}`\n\n"
            f"¿Deseas comprimir este video?",
            reply_to_message_id=message.id,  # Respuesta al video original
            reply_markup=keyboard
        )
        
        logger.info(f"Solicitud de confirmación creada para {user_id}: {message.video.file_name}")
    except Exception as e:
        logger.error(f"Error en handle_video: {e}", exc_info=True)

@app.on_message(filters.text)
async def handle_message(client, message):
    try:
        text = message.text
        username = message.from_user.username
        chat_id = message.chat.id
        user_id = message.from_user.id

        if user_id in ban_users:
            return
            
        logger.info(f"Mensaje recibido de {user_id}: {text}")

        if text.startswith(('/calidad', '.calidad', '/quality', '.quality')):
            await calidad_command(client, message)
        elif text.startswith(('/resetcalidad', '.resetcalidad')):
            await reset_calidad_command(client, message)
        elif text.startswith(('/settings', '.settings')):
            await settings_menu(client, message)
        elif text.startswith(('/banuser', '.banuser', '/deluser', '.deluser')):
            if user_id in admin_users:
                await ban_or_delete_user_command(client, message)
            else:
                logger.warning(f"Intento no autorizado de banuser/deluser por {user_id}")
        elif text.startswith(('/cola', '.cola')):
            if user_id in admin_users:
                await ver_cola_command(client, message)
        elif text.startswith(('/auto', '.auto')):
            if user_id in admin_users:
                await startup_command(client, message)
        elif text.startswith(('/myplan', '.myplan')):
            await my_plan_command(client, message)
        elif text.startswith(('/setplan', '.setplan')):
            if user_id in admin_users:
                await set_plan_command(client, message)
        elif text.startswith(('/userinfo', '.userinfo')):
            if user_id in admin_users:
                await user_info_command(client, message)
        elif text.startswith(('/planes', '.planes')):
            await planes_command(client, message)
        elif text.startswith(('/generatekey', '.generatekey')):
            if user_id in admin_users:
                await generate_key_command(client, message)
        elif text.startswith(('/listkeys', '.listkeys')):
            if user_id in admin_users:
                await list_keys_command(client, message)
        elif text.startswith(('/delkeys', '.delkeys')):
            if user_id in admin_users:
                await del_keys_command(client, message)
        elif text.startswith(('/user', '.user')):
            if user_id in admin_users:
                await list_users_command(client, message)
        elif text.startswith(('/admin', '.admin')):
            if user_id in admin_users:
                await admin_stats_command(client, message)
        elif text.startswith(('/restuser', '.restuser')):
            if user_id in admin_users:
                await reset_all_users_command(client, message)
        elif text.startswith(('/desuser', '.desuser')):
            if user_id in admin_users:
                await unban_user_command(client, message)
        elif text.startswith(('/deleteuser', '.deleteuser')):
            if user_id in admin_users:
                await delete_user_command(client, message)
        elif text.startswith(('/viewban', '.viewban')):
            if user_id in admin_users:
                await view_banned_users_command(client, message)
        elif text.startswith(('/msg', '.msg')):
            if user_id in admin_users:
                await broadcast_command(client, message)
        elif text.startswith(('/cancel', '.cancel')):
            await cancel_command(client, message)
        elif text.startswith(('/cancelqueue', '.cancelqueue')):
            await cancel_queue_command(client, message)
        elif text.startswith(('/key', '.key')):
            await key_command(client, message)
        elif text.startswith(('/restart', '.restart')):
            if user_id in admin_users:
                await restart_command(client, message)
        elif text.startswith(('/getdb', '.getdb')):
            if user_id in admin_users:
                await get_db_command(client, message)
        elif text.startswith(('/restdb', '.restdb')):
            if user_id in admin_users:
                await rest_db_command(client, message)

        if message.reply_to_message:
            original_message = sent_messages.get(message.reply_to_message.id)
            if original_message:
                user_id = original_message["user_id"]
                sender_info = f"Respuesta de @{message.from_user.username}" if message.from_user.username else f"Respuesta de user ID: {message.from_user.id}"
                await send_protected_message(user_id, f"{sender_info}: {message.text}")
                logger.info(f"Respuesta enviada a {user_id}")
    except Exception as e:
        logger.error(f"Error en handle_message: {e}", exc_info=True)

# ======================== FUNCIONES AUXILIARES ======================== #

async def notify_group(client, message: Message, original_size: int, compressed_size: int = None, status: str = "start"):
    try:
        group_id = -4826894501  # Reemplaza con tu ID de grupo

        user = message.from_user
        username = f"@{user.username}" if user.username else "Sin username"
        file_name = message.video.file_name or "Sin nombre"
        size_mb = original_size // (1024 * 1024)

        if status == "start":
            text = (
                "📤 **Nuevo video recibido para comprimir**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )
        elif status == "done":
            compressed_mb = compressed_size // (1024 * 1024)
            text = (
                "📥 **Video comprimido y enviado**\n\n"
                f"👤 **Usuario:** {username}\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"📦 **Tamaño original:** {size_mb} MB\n"
                f"📉 **Tamaño comprimido:** {compressed_mb} MB\n"
                f"📁 **Nombre:** `{file_name}`"
            )

        await app.send_message(chat_id=group_id, text=text)
        logger.info(f"Notificación enviada al grupo: {user.id} - {file_name} ({status})")
    except Exception as e:
        logger.error(f"Error enviando notificación al grupo: {e}")

# ======================== INICIO DEL BOT ======================== #

try:
    logger.info("Iniciando el bot...")
    app.run()
except Exception as e:
    logger.critical(f"Error fatal al iniciar el bot: {e}", exc_info=True)