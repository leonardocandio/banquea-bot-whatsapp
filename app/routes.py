from fastapi import APIRouter, Depends, HTTPException, Form, Body, BackgroundTasks, Request, Query
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional, List
import logging
import re
import httpx
import time
import json
import functools
from datetime import datetime
import uuid
import os

from . import crud, schemas, models
from .database import get_db
from .whatsapp import WhatsAppClient
from .utils import load_questions_from_csv, process_user_response, get_random_question, format_question_message, process_user_response_from_list

router = APIRouter()
whatsapp_client = WhatsAppClient()
logger = logging.getLogger(__name__)

# State machine for user conversation
STATES = {
    "INITIAL": 0,
    "AWAITING_CONFIRMATION": 1,
    "AWAITING_DAY": 2,
    "AWAITING_HOUR": 3,
    "SUBSCRIBED": 4,
    "AWAITING_QUESTION_RESPONSE": 5
}

user_states = {}  # In-memory state storage. In production, use a database or Redis

# Create a decorator to log execution time
def log_execution_time(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        request_id = f"req_{int(time.time())}"
        start_time = time.time()
        
        # Log start of execution
        logger.info(f"[{request_id}] Starting execution of {func.__name__} at {datetime.now().isoformat()}")
        
        try:
            # Pass request_id to the function if it accepts it
            if 'request_id' in func.__code__.co_varnames:
                kwargs['request_id'] = request_id
                result = await func(*args, **kwargs)
            else:
                result = await func(*args, **kwargs)
            
            # Log successful completion
            end_time = time.time()
            execution_time = end_time - start_time
            logger.info(f"[{request_id}] Successfully completed {func.__name__} in {execution_time:.3f}s")
            
            # Add request_id to result if it's a dict
            if isinstance(result, dict):
                result['request_id'] = request_id
                
            return result
            
        except Exception as e:
            # Log error
            end_time = time.time()
            execution_time = end_time - start_time
            logger.error(f"[{request_id}] Error in {func.__name__} after {execution_time:.3f}s: {str(e)}", exc_info=True)
            raise
            
    return wrapper

def get_user_state(phone_number: str) -> Dict[str, Any]:
    """Get or initialize user state"""
    if phone_number not in user_states:
        user_states[phone_number] = {
            "state": STATES["INITIAL"],
            "temp_data": {}
        }
    return user_states[phone_number]

def set_user_state(phone_number: str, state: int, temp_data: Optional[Dict[str, Any]] = None):
    """Update user state"""
    user_states[phone_number] = {
        "state": state,
        "temp_data": temp_data or {}
    }

@router.get("/")
def read_root():
    """Root endpoint for health check"""
    return {"status": "ok", "message": "Banquea WhatsApp Bot API is running"}

@router.get("/debug/user-state/{phone_number}")
def get_user_state_endpoint(phone_number: str):
    """Debug endpoint to check a user's state"""
    state = get_user_state(phone_number)
    return {
        "phone_number": phone_number,
        "state": state.get("state"),
        "state_name": next((k for k, v in STATES.items() if v == state.get("state")), "UNKNOWN"),
        "temp_data": state.get("temp_data", {})
    }

@router.get("/webhook")
async def verify_webhook(request: Request):
    """
    Handle the webhook verification request from WhatsApp Cloud API.
    This is required when setting up the webhook in the Meta Developer Portal.
    """
    # Parse params from the webhook verification request
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    # Log verification attempt
    logger.info(f"Webhook verification attempt - Mode: {mode}, Token: {token}, Challenge: {challenge}")
    
    # Check if a token and mode were sent
    if mode and token:
        # Check the mode and token sent are correct
        verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "banquea_medical_bot_verify_token")
        if mode == "subscribe" and token == verify_token:
            # Respond with 200 OK and challenge token from the request
            logger.info(f"WEBHOOK_VERIFIED - responding with challenge: {challenge}")
            return int(challenge)
        else:
            # Responds with '403 Forbidden' if verify tokens do not match
            logger.error(f"VERIFICATION_FAILED - token mismatch. Expected: {verify_token}, Got: {token}")
            raise HTTPException(status_code=403, detail="Verification failed")
    else:
        # Responds with '400 Bad Request' if verify tokens do not match
        logger.error("MISSING_PARAMETER - hub.mode or hub.verify_token missing")
        raise HTTPException(status_code=400, detail="Missing parameters")

@router.post("/webhook")
@log_execution_time
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    request_id: str = None
):
    """
    Webhook for WhatsApp messages.
    """
    if not request_id:
        request_id = str(uuid.uuid4())
    
    # Log detailed request information
    logger.info(f"[{request_id}] Webhook request received - Method: {request.method}, Path: {request.url.path}")
    logger.info(f"[{request_id}] Request headers: {dict(request.headers)}")
    
    try:
        # Get raw request body first for debugging
        raw_body = await request.body()
        logger.info(f"[{request_id}] Raw request body: {raw_body}")
        
        # Parse as JSON for normal processing
        payload = await request.json()
        logger.info(f"[{request_id}] Received webhook payload: {json.dumps(payload)}")
        
        # Process the webhook payload
        message_data = whatsapp_client.process_webhook_payload(payload)
        
        if not message_data:
            # Not a valid message event or no messages in the payload
            logger.warning(f"[{request_id}] No processable messages in payload")
            return {"status": "success", "message": "No processable messages"}
        
        # Extract data from the message
        phone_number = message_data.get("from_number", "")
        message_body = message_data.get("body", "")
        message_type = message_data.get("message_type", "")
        message_id = message_data.get("message_id", "")
        interactive_data = message_data.get("interactive_data", {})
        
        if not phone_number:
            logger.warning(f"[{request_id}] Incomplete message data: missing phone number")
            return {"status": "success", "message": "Incomplete message data"}
        
        # Enhanced logging for debugging
        logger.info(f"[{request_id}] Processing message - From: {phone_number}, ID: {message_id}, Type: {message_type}, Content: {message_body}")
        if interactive_data:
            logger.info(f"[{request_id}] Interactive data: {json.dumps(interactive_data)}")

        # Get or create user
        user = crud.get_user_by_phone(db, phone_number)
        if not user:
            user = crud.create_user(db, phone_number)
            logger.info(f"[{request_id}] Created new user with phone number: {phone_number}, ID: {user.id}")
        else:
            logger.info(f"[{request_id}] Existing user found - Phone: {phone_number}, ID: {user.id}, Active: {user.is_active}")

        # Get user state
        user_state = get_user_state(phone_number)
        current_state = user_state["state"]
        state_name = next((k for k, v in STATES.items() if v == current_state), "UNKNOWN")
        logger.info(f"[{request_id}] Current user state: {state_name} ({current_state}), Temp data: {user_state.get('temp_data', {})}")
        
        # Process message based on state
        response_message = ""
        
        # Handle interactive messages
        if message_type == "interactive" and interactive_data:
            logger.info(f"[{request_id}] Processing interactive message for state {state_name}")
            
            # Get the reply type
            reply_type = interactive_data.get("reply_type", "")
            button_id = interactive_data.get("id", "")
            title = interactive_data.get("title", "")
            
            logger.info(f"[{request_id}] Interactive details - Type: {reply_type}, ID: {button_id}, Title: {title}")
            
            # Handle button replies
            if reply_type == "button_reply":
                if button_id == "yes_button":
                    # User clicked "Yes" button, send day selection
                    logger.info(f"[{request_id}] User clicked Yes button, sending day selection")
                    await send_day_selection_message(
                        phone_number, 
                        "Por favor selecciona el día de la semana en que deseas recibir las preguntas:"
                    )
                    set_user_state(phone_number, STATES["AWAITING_DAY"])
                    # Log state transition
                    logger.info(f"[{request_id}] State transition: {state_name} -> AWAITING_DAY")
                    return {"status": "success"}
                elif button_id == "no_button":
                    # User clicked "No" button
                    response_message = "Entendido. Si cambias de opinión, escribe INICIAR en cualquier momento."
                    crud.deactivate_user(db, user.id)
                    set_user_state(phone_number, STATES["INITIAL"])
                    # Log state transition and user deactivation
                    logger.info(f"[{request_id}] User deactivated - ID: {user.id}")
                    logger.info(f"[{request_id}] State transition: {state_name} -> INITIAL")
            
            # Handle list selection replies
            elif reply_type == "list_reply":
                if current_state == STATES["AWAITING_DAY"]:
                    # User selected a day from the list
                    day_id = button_id
                    day_title = title
                    
                    logger.info(f"[{request_id}] User selected day: {day_title} (ID: {day_id})")
                    
                    # Handle day selection and immediately send a question
                    result = await handle_day_selection(
                        phone_number,
                        day_id,
                        day_title,
                        user.id,
                        db,
                        request_id
                    )
                    
                    if result:
                        return {"status": "success"}
                    
                elif current_state == STATES["AWAITING_QUESTION_RESPONSE"]:
                    # User selected an answer from a question list
                    option_id = button_id  # Format: q_{question_id}_opt_{option_number}
                    
                    logger.info(f"[{request_id}] User selected answer: {title} (ID: {option_id})")
                    
                    # Extract option number from the ID
                    try:
                        # Parse option number from q_{question_id}_opt_{option_number}
                        parts = option_id.split('_')
                        question_id = int(parts[1])
                        option_num = int(parts[3])
                        
                        # Process the response
                        is_correct, feedback = process_user_response_from_list(db, user.id, question_id, option_num)
                        
                        # Send feedback message
                        days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
                        day_name = days[user.preferred_day]
                        
                        response_message = f"{feedback}\n\nRecibirás la próxima pregunta el {day_name} de la próxima semana."
                        await whatsapp_client.send_message(phone_number, response_message)
                        
                        # Update user state
                        set_user_state(phone_number, STATES["SUBSCRIBED"])
                        
                        return {"status": "success"}
                    except Exception as e:
                        logger.error(f"[{request_id}] Error processing question response: {str(e)}")
                        await whatsapp_client.send_message(
                            phone_number,
                            "Lo sentimos, hubo un error al procesar tu respuesta. Por favor intenta de nuevo."
                        )
                        return {"status": "error", "error": str(e)}
        
        # Normal text message processing
        if current_state == STATES["INITIAL"]:
            # First contact with the user or user in initial state
            # Send button message instead of just text
            logger.info(f"[{request_id}] Sending welcome message with buttons")
            send_result = await send_simple_button_message(
                phone_number,
                "Bienvenido/a al bot de preguntas médicas de Banquea. Este bot te enviará preguntas semanales para reforzar tus conocimientos médicos. ¿Deseas recibir preguntas semanales?",
                "Configuración de suscripción"
            )
            logger.info(f"[{request_id}] Button message send result: {send_result}")
            
            set_user_state(phone_number, STATES["AWAITING_CONFIRMATION"])
            logger.info(f"[{request_id}] State transition: {state_name} -> AWAITING_CONFIRMATION")
            
            return {"status": "success"}
            
        elif current_state == STATES["AWAITING_CONFIRMATION"]:
            # User responding to confirmation
            if message_body.lower() in ["si", "sí", "yes", "y"]:
                # Create a list message for day selection
                logger.info(f"[{request_id}] User confirmed subscription, sending day selection message")
                await send_day_selection_message(
                    phone_number, 
                    "Por favor selecciona el día de la semana en que deseas recibir las preguntas:"
                )
                set_user_state(phone_number, STATES["AWAITING_DAY"])
                logger.info(f"[{request_id}] State transition: {state_name} -> AWAITING_DAY")
                
                return {"status": "success"}
            elif message_body.lower() in ["no", "n"]:
                response_message = "Entendido. No recibirás preguntas semanales. Si cambias de opinión, escribe INICIAR."
                crud.deactivate_user(db, user.id)
                set_user_state(phone_number, STATES["INITIAL"])
                logger.info(f"[{request_id}] User declined subscription and was deactivated")
                logger.info(f"[{request_id}] State transition: {state_name} -> INITIAL")
            else:
                response_message = "No entendí tu respuesta. Por favor responde SI o NO, o usa los botones enviados."
                logger.info(f"[{request_id}] User provided unrecognized confirmation response: {message_body}")
                
        elif current_state == STATES["AWAITING_DAY"]:
            # If we get here, it means the user responded with text instead of using the list
            try:
                day = int(message_body.strip())
                if 1 <= day <= 7:
                    # Convert to 0-6 format where 0 is Monday
                    day_value = day - 1
                    
                    # Store temporarily
                    temp_data = user_state["temp_data"]
                    temp_data["preferred_day"] = day_value
                    
                    response_message = (
                        f"Has seleccionado el día {day}. "
                        "¿A qué hora prefieres recibir las preguntas? "
                        "Responde con un número del 0 al 23 (formato 24 horas)."
                    )
                    set_user_state(phone_number, STATES["AWAITING_HOUR"], temp_data)
                    logger.info(f"[{request_id}] Updated user state to AWAITING_HOUR with day value {day_value}")
                else:
                    response_message = "Por favor, elige un número del 1 al 7, donde 1 es lunes y 7 es domingo."
            except ValueError:
                # Not a number, let's try to interpret the day name
                day_map = {
                    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, 
                    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
                }
                
                day_value = day_map.get(message_body.lower())
                
                if day_value is not None:
                    # Store the day value
                    temp_data = user_state["temp_data"]
                    temp_data["preferred_day"] = day_value
                    
                    response_message = (
                        f"Has seleccionado {message_body}. "
                        "¿A qué hora prefieres recibir las preguntas? "
                        "Responde con un número del 0 al 23 (formato 24 horas)."
                    )
                    set_user_state(phone_number, STATES["AWAITING_HOUR"], temp_data)
                    logger.info(f"[{request_id}] Updated user state to AWAITING_HOUR with day value {day_value}")
                else:
                    # Send the day selection list again
                    await send_day_selection_message(
                        phone_number, 
                        "No pude entender tu selección. Por favor, selecciona un día de la semana:"
                    )
                    return {"status": "success"}
                
        elif current_state == STATES["AWAITING_HOUR"]:
            # User responding with preferred hour
            try:
                hour = int(message_body.strip())
                if 0 <= hour <= 23:
                    temp_data = user_state["temp_data"]
                    preferred_day = temp_data.get("preferred_day", 0)
                    
                    # Update user preferences
                    crud.update_user_preferences(db, user.id, preferred_day, hour)
                    logger.info(f"[{request_id}] Updated user preferences: day={preferred_day}, hour={hour}")
                    
                    # Days mapped to names for better UX
                    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
                    day_name = days[preferred_day]
                    
                    response_message = (
                        f"¡Perfecto! Recibirás preguntas médicas cada {day_name} a las {hour}:00 horas. "
                        "Para dejar de recibir preguntas, escribe DETENER en cualquier momento."
                    )
                    set_user_state(phone_number, STATES["SUBSCRIBED"])
                    
                    # Send a sample question right away to demonstrate
                    background_tasks.add_task(send_sample_question, phone_number, db)
                else:
                    response_message = "Por favor, elige un número del 0 al 23."
            except ValueError:
                response_message = "Por favor, responde con un número del 0 al 23."
                
        elif current_state == STATES["SUBSCRIBED"] or current_state == STATES["AWAITING_QUESTION_RESPONSE"]:
            # User already subscribed, check for commands or answering a question
            if message_body.lower() in ["detener", "stop", "unsubscribe"]:
                crud.deactivate_user(db, user.id)
                response_message = "Has cancelado tu suscripción. Ya no recibirás preguntas médicas. Para volver a suscribirte, escribe INICIAR."
                set_user_state(phone_number, STATES["INITIAL"])
            elif message_body.lower() in ["iniciar", "start", "subscribe"]:
                # Send the welcome button message again
                await send_simple_button_message(
                    phone_number,
                    "¿Deseas recibir preguntas médicas semanales para reforzar tus conocimientos?",
                    "Configuración de suscripción"
                )
                set_user_state(phone_number, STATES["AWAITING_CONFIRMATION"])
                return {"status": "success"}
            elif re.match(r'^\d+$', message_body.strip()) and current_state == STATES["AWAITING_QUESTION_RESPONSE"]:
                # User is responding to a question
                is_correct, feedback = process_user_response(db, user.id, message_body)
                response_message = feedback
                set_user_state(phone_number, STATES["SUBSCRIBED"])
            else:
                response_message = (
                    "Recuerda que puedes utilizar estos comandos:\n"
                    "DETENER - para dejar de recibir preguntas\n"
                    "INICIAR - para configurar de nuevo tus preferencias"
                )
        
        # Send response to the user
        if response_message:
            logger.info(f"[{request_id}] Sending response: {response_message}")
            send_result = await whatsapp_client.send_message(phone_number, response_message)
            logger.info(f"[{request_id}] Message send result: {send_result}")
        
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"[{request_id}] Error processing webhook: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}

@log_execution_time
async def send_sample_question(phone_number: str, db: Session, request_id: str = None):
    """
    Send a sample question to demonstrate the bot functionality
    
    Args:
        phone_number: The user's phone number
        db: Database session
        request_id: Request ID for consistent logging
    """
    logger.info(f"[{request_id}] Preparing sample question for {phone_number}")
    
    try:
        # Get a random question from in-memory store
        question = get_random_question()
        
        if not question:
            # No questions in memory, send a default one
            logger.info(f"[{request_id}] No questions found in memory, using default question")
            question_text = (
                "Pregunta de ejemplo: ¿Cuál de las siguientes condiciones es una contraindicación absoluta para el uso de trombolíticos en un paciente con infarto agudo de miocardio?\n\n"
                "1) Hipertensión arterial controlada\n"
                "2) Sangrado intracraneal previo\n"
                "3) Diabetes mellitus\n"
                "4) Edad mayor de 75 años"
            )
            # Correct answer would be 2
        else:
            # Format the question with options
            logger.info(f"[{request_id}] Using question ID: {question['id']}, Category: {question['area']}")
            question_text = format_question_message(question)
        
        # Get the user from the database
        user = crud.get_user_by_phone(db, phone_number)
        if user:
            # Set user state to waiting for question response
            set_user_state(phone_number, STATES["AWAITING_QUESTION_RESPONSE"])
            logger.info(f"[{request_id}] Set user state to AWAITING_QUESTION_RESPONSE")
            
            # Prepare message with question
            if not question:
                message = f"¡Aquí tienes una pregunta de ejemplo!\n\n{question_text}"
            else:
                message = f"¡Aquí tienes una pregunta de ejemplo!\n\n{question_text}"
            
            # Send the question
            logger.info(f"[{request_id}] Sending question to {phone_number}")
            result = await whatsapp_client.send_message(
                phone_number,
                message
            )
            
            logger.info(f"[{request_id}] Question message send result: {result}")
            return result
        else:
            logger.warning(f"[{request_id}] User not found for phone number: {phone_number}")
            return False
    
    except Exception as e:
        logger.error(f"[{request_id}] Error sending sample question: {str(e)}", exc_info=True)
        return False

@log_execution_time
async def send_day_selection_message(phone_number: str, message: str, request_id: str = None):
    """
    Send a day selection list message to the user.
    
    Args:
        phone_number: The user's phone number
        message: The message text to send with the list
        request_id: Request ID for consistent logging
    """
    logger.info(f"[{request_id}] Preparing day selection message for {phone_number}")
    
    # Create the sections array for the interactive list
    sections = [
        {
            "title": "Días de la semana",
            "rows": [
                {"id": "day_1", "title": "Lunes", "description": "Primer día de la semana"},
                {"id": "day_2", "title": "Martes", "description": "Segundo día de la semana"},
                {"id": "day_3", "title": "Miércoles", "description": "Tercer día de la semana"},
                {"id": "day_4", "title": "Jueves", "description": "Cuarto día de la semana"},
                {"id": "day_5", "title": "Viernes", "description": "Quinto día de la semana"},
                {"id": "day_6", "title": "Sábado", "description": "Sexto día de la semana"},
                {"id": "day_7", "title": "Domingo", "description": "Séptimo día de la semana"}
            ]
        }
    ]
    
    logger.info(f"[{request_id}] Sending interactive list message with {len(sections[0]['rows'])} options")
    
    # Send the interactive list message
    result = await whatsapp_client.send_interactive_list_message(
        to_number=phone_number,
        header_text="Selección de día",
        body_text=message,
        button_text="Ver días",
        sections=sections,
        footer_text="Banquea - Bot de preguntas médicas"
    )
    
    logger.info(f"[{request_id}] Interactive list message send result: {result}")
    return result

@router.post("/admin/blacklist/{phone_number}")
def blacklist_user_endpoint(
    phone_number: str,
    db: Session = Depends(get_db)
):
    """Admin endpoint to blacklist a user"""
    user = crud.get_user_by_phone(db, phone_number)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    crud.blacklist_user(db, user.id)
    return {"status": "success", "message": f"User {phone_number} has been blacklisted"}

@router.post("/admin/load-questions")
def load_questions():
    """Admin endpoint to load questions from CSV files into memory"""
    try:
        load_questions_from_csv()
        return {"status": "success", "message": "Questions loaded successfully"}
    except Exception as e:
        logger.error(f"Error loading questions: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error loading questions: {str(e)}")

@router.get("/admin/users")
def get_all_users(
    db: Session = Depends(get_db)
):
    """Admin endpoint to get all users"""
    users = db.query(models.User).all()
    return users

@router.post("/admin/add-test-user")
async def add_test_user(
    phone_number: str = Body(...),
    template_name: str = Body(...),
    language_code: str = Body("es"),
    db: Session = Depends(get_db)
):
    """
    Add a test user and send a template message to them.
    This is useful for testing the WhatsApp integration.
    
    Args:
        phone_number: The user's phone number with country code (e.g., +51973296571)
        template_name: The name of the template to send
        language_code: The language code for the template (default: es)
    """
    try:
        # Clean up phone number
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"
        
        # Create or get user
        user = crud.get_user_by_phone(db, phone_number)
        if not user:
            user = crud.create_user(db, phone_number)
            logger.info(f"Created new test user with phone number: {phone_number}")
        else:
            logger.info(f"Using existing user with phone number: {phone_number}")
        
        # Send template message
        success = await whatsapp_client.send_template_message(
            phone_number,
            template_name,
            language_code
        )
        
        if success:
            return {
                "status": "success",
                "message": f"Template message '{template_name}' sent successfully to {phone_number}",
                "user_id": user.id
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to send template message to {phone_number}"
            }
            
    except Exception as e:
        logger.error(f"Error adding test user: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error adding test user: {str(e)}")

@router.post("/admin/send-day-selection")
async def send_day_selection_test(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Send a day selection list to a test user.
    
    Args:
        request: FastAPI request object
        db: Database session
    """
    try:
        # Get JSON from request body
        body = await request.json()
        phone_number = body.get("phone_number")
        
        if not phone_number:
            raise HTTPException(status_code=400, detail="phone_number is required")
        
        # Clean up phone number
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"
        
        # Create or get user
        user = crud.get_user_by_phone(db, phone_number)
        if not user:
            user = crud.create_user(db, phone_number)
            logger.info(f"Created new test user with phone number: {phone_number}")
        else:
            logger.info(f"Using existing user with phone number: {phone_number}")
        
        # Send day selection message
        await send_day_selection_message(
            phone_number,
            "Por favor selecciona el día de la semana en que deseas recibir las preguntas:"
        )
        
        return {
            "status": "success",
            "message": f"Day selection message sent successfully to {phone_number}",
            "user_id": user.id
        }
            
    except Exception as e:
        logger.error(f"Error sending day selection: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error sending day selection: {str(e)}")

@router.post("/admin/send-button-message")
async def send_button_message_test(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Send a button message to a test user.
    
    Args:
        request: FastAPI request object
        db: Database session
    """
    try:
        # Get JSON from request body
        body = await request.json()
        phone_number = body.get("phone_number")
        
        if not phone_number:
            raise HTTPException(status_code=400, detail="phone_number is required")
        
        # Clean up phone number
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"
        
        # Create or get user
        user = crud.get_user_by_phone(db, phone_number)
        if not user:
            user = crud.create_user(db, phone_number)
            logger.info(f"Created new test user with phone number: {phone_number}")
        else:
            logger.info(f"Using existing user with phone number: {phone_number}")
        
        # Send button message
        await send_simple_button_message(
            phone_number,
            "¿Estás listo para comenzar con las preguntas médicas?",
            "Configuración de preguntas"
        )
        
        return {
            "status": "success",
            "message": f"Button message sent successfully to {phone_number}",
            "user_id": user.id
        }
            
    except Exception as e:
        logger.error(f"Error sending button message: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error sending button message: {str(e)}")

@log_execution_time
async def send_simple_button_message(phone_number: str, body_text: str, header_text: Optional[str] = None, request_id: str = None):
    """
    Send a simple button message with Yes/No options
    
    Args:
        phone_number: The user's phone number
        body_text: The main message text
        header_text: Optional header text
        request_id: Request ID for consistent logging
    """
    logger.info(f"[{request_id}] Preparing button message for {phone_number}")
    
    # Create the buttons
    buttons = [
        {
            "type": "reply",
            "reply": {
                "id": "yes_button",
                "title": "Sí"
            }
        },
        {
            "type": "reply",
            "reply": {
                "id": "no_button",
                "title": "No"
            }
        }
    ]
    
    logger.info(f"[{request_id}] Sending button message with {len(buttons)} options")
    
    # Send the button message
    result = await whatsapp_client.send_button_message(
        to_number=phone_number,
        body_text=body_text,
        buttons=buttons,
        header_text=header_text,
        footer_text="Banquea - Bot de preguntas médicas"
    )
    
    logger.info(f"[{request_id}] Button message send result: {result}")
    return result

async def handle_day_selection(phone_number: str, day_id: str, day_title: str, user_id: int, db: Session, request_id: str = None):
    """
    Handle day selection and go directly to sending a question.
    
    Args:
        phone_number: The user's phone number
        day_id: The selected day ID
        day_title: The selected day title
        user_id: The user's ID
        db: Database session
        request_id: Request ID for consistent logging
    """
    try:
        # Convert day name to day index (0 = Monday, 6 = Sunday)
        day_map = {
            "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, 
            "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
        }
        
        day_value = day_map.get(day_title.lower())
        
        if day_value is None:
            logger.error(f"[{request_id}] Invalid day selected: {day_title}")
            await whatsapp_client.send_message(
                phone_number,
                "Lo siento, hubo un error con tu selección. Por favor intenta de nuevo."
            )
            return False
            
        # Update user preferences (set default hour to 9)
        crud.update_user_preferences(db, user_id, day_value, 9)  # Default to 9 AM
        logger.info(f"[{request_id}] Updated user preferences: day={day_value}, hour=9 (default)")
        
        # Get a random question
        question = get_random_question()
        if not question:
            logger.error(f"[{request_id}] No questions available")
            await whatsapp_client.send_message(
                phone_number,
                "Lo sentimos, no pudimos encontrar preguntas disponibles. Por favor intenta más tarde."
            )
            return False
        
        # Store the current question ID for the user
        user = crud.get_user_by_id(db, user_id)
        user.last_question_id = question["id"]
        user.last_message_sent = datetime.utcnow()
        db.commit()
        
        # Send the question as an interactive list
        result = await whatsapp_client.send_question_list_message(
            phone_number,
            question["text"],
            question["options"],
            question["id"]
        )
        
        if result:
            # Set user state to waiting for question response
            set_user_state(phone_number, STATES["AWAITING_QUESTION_RESPONSE"])
            logger.info(f"[{request_id}] Set user state to AWAITING_QUESTION_RESPONSE")
            return True
        else:
            logger.error(f"[{request_id}] Failed to send question to {phone_number}")
            return False
            
    except Exception as e:
        logger.error(f"[{request_id}] Error handling day selection: {str(e)}")
        return False

@router.post("/admin/send-question-to-all")
async def send_question_to_all_users(
    db: Session = Depends(get_db)
):
    """Admin endpoint to force send a question to all active users"""
    try:
        users = db.query(models.User).filter(
            models.User.is_active == True,
            models.User.is_blacklisted == False
        ).all()
        
        success_count = 0
        fail_count = 0
        
        for user in users:
            # Get a random question
            question = get_random_question()
            if not question:
                logger.error("No questions available in memory")
                continue
            
            # Send the question
            result = await whatsapp_client.send_question_list_message(
                user.phone_number,
                question["text"],
                question["options"],
                question["id"]
            )
            
            if result:
                # Update user info
                user.last_message_sent = datetime.utcnow()
                user.last_question_id = question["id"]
                db.commit()
                
                # Set user state to waiting for question response
                set_user_state(user.phone_number, STATES["AWAITING_QUESTION_RESPONSE"])
                
                success_count += 1
            else:
                fail_count += 1
        
        return {
            "status": "success", 
            "message": f"Question sent to {success_count} users. Failed: {fail_count}"
        }
    except Exception as e:
        logger.error(f"Error sending questions to all users: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) 