import random
from datetime import datetime, timedelta
from typing import Union
from uuid import uuid4

import fastapi
import jwt as JWT
import passlib.hash as _hash
from fastapi import BackgroundTasks, Cookie
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import and_, orm

from bigfastapi.api_key import check_api_key
from bigfastapi.core.helpers import Helpers

# from fastapi.security import OAuth2PasswordBearer
from bigfastapi.custom_oauth import OAuth2PasswordBearer
from bigfastapi.db import database
from bigfastapi.db.database import get_db
from bigfastapi.utils import settings, utils

from ..models import auth_models, user_models
from ..schemas import auth_schemas, users_schemas
from ..services import email_services

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

JWT_SECRET = settings.JWT_SECRET
ALGORITHM = "HS256"


async def find_user_by_email(email: str, db: orm.Session):
    found_user = (
        db.query(user_models.User).filter(user_models.User.email == email).first()
    )
    return {
        "user": found_user,
        "response_user": auth_schemas.UserCreateOut.from_orm(found_user),
    }


async def find_user_by_phone(
    phone_number: str, phone_country_code: str, db: orm.Session
):
    found_user = (
        db.query(user_models.User)
        .filter(
            and_(
                user_models.User.phone_number == phone_number,
                user_models.User.phone_country_code == phone_country_code,
            )
        )
        .first()
    )

    return {
        "user": found_user,
        "response_user": auth_schemas.UserCreateOut.from_orm(found_user),
    }


# APPROACH SUBJECT TO REVIEW!
async def create_user(
    user: auth_schemas.UserCreate, db: orm.Session, is_su: bool = False
):
    su_status = True if is_su else False

    # Validate email and phone input fields
    validate_email_and_phone_fields(user)

    default_user = { "user": None, "response_user": None }
    existing_user_with_email = default_user
    existing_user_with_phone = default_user

    if user.email:
        existing_user_with_email = await find_user_by_email(email=user.email, db=db)

        if existing_user_with_email["user"] is not None:
            raise fastapi.HTTPException(
                status_code=403, detail="An account with this email already exist"
            )

    if user.phone_number:
        existing_user_with_phone = await find_user_by_phone(
            phone_number=user.phone_number,
            phone_country_code=user.phone_country_code,
            db=db,
        )

        if existing_user_with_phone["user"] is not None:
            raise fastapi.HTTPException(
                status_code=403,
                detail="An account with this phone number already exist",
            )

    if existing_user_with_email["user"] is None:
        # proceed with account creation with email
        user_obj = user_models.User(
            id=uuid4().hex,
            email=user.email,
            password_hash=_hash.sha256_crypt.hash(user.password),
            first_name=user.first_name,
            last_name=user.last_name,
            phone_number=user.phone_number,
            is_active=True,
            is_verified=True,
            is_superuser=su_status,
            phone_country_code=user.phone_country_code,
            is_deleted=False,
            google_id=user.google_id,
            google_image_url=user.google_image_url,
            image_url=user.image_url,
            device_id=user.device_id,
        )

        db.add(user_obj)
        db.commit()
        db.refresh(user_obj)

        return auth_schemas.UserCreateOut.from_orm(user_obj)

    if existing_user_with_phone["user"] is None:
        # proceed to account creation with phone
        user_obj = user_models.User(
            id=uuid4().hex,
            email=user.email,
            password_hash=_hash.sha256_crypt.hash(user.password),
            first_name=user.first_name,
            last_name=user.last_name,
            phone_number=user.phone_number,
            is_active=True,
            is_verified=True,
            is_superuser=su_status,
            phone_country_code=user.phone_country_code,
            is_deleted=False,
            google_id=user.google_id,
            google_image_url=user.google_image_url,
            image_url=user.image_url,
            device_id=user.device_id,
        )

        db.add(user_obj)
        db.commit()
        db.refresh(user_obj)

        return auth_schemas.UserCreateOut.from_orm(user_obj)


def validate_email_and_phone_fields(user: auth_schemas.UserCreate):
    if user.email is None and user.phone_number is None:
        raise fastapi.HTTPException(
            status_code=403,
            detail="You must use a either phone_number or email to sign up",
        )

    # Validate phone number
    if user.phone_number and user.phone_country_code is None:
        raise fastapi.HTTPException(
            status_code=422,
            detail="Country code is required when a phone number is specified",
        )

    if user.phone_number and user.phone_country_code:
        check_country_code = utils.validate_phone_dialcode(user.phone_country_code)
        if check_country_code is None:
            raise fastapi.HTTPException(status_code=403, detail="Invalid country code")
    if user.phone_number is None and user.phone_country_code:
        raise fastapi.HTTPException(
            status_code=422,
            detail="You must add a phone number when you add a country code",
        )


def send_slack_notification_for_auth(user, action: str = "login"):
    message = f"New {action} from {user['user'].email}"

    Helpers.slack_notification("LOG_WEBHOOK_URL", text=message)


async def create_access_token(data: dict, db: orm.Session):
    """Generate access token for a user"""
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    token_obj = auth_models.Token(
        id=uuid4().hex, user_id=data["user_id"], token=encoded_jwt
    )
    db.add(token_obj)
    db.commit()
    db.refresh(token_obj)
    return encoded_jwt


async def create_refresh_token(data: dict, db: orm.Session):
    """Generate refresh token for a user"""
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(minutes=2880)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

    return encoded_jwt


def verify_refresh_token(refresh_token: str, credentials_exception, db: orm.Session):
    """Verify an assigned refresh token"""
    try:
        if not refresh_token:
            raise fastapi.HTTPException(status_code=401, detail="expired or invalid login token")

        payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[ALGORITHM])
        id: str = payload.get("user_id")

        user = db.query(user_models.User).filter(user_models.User.id == id).first()

        email = user.email

        if id is None:
            raise credentials_exception
        token_data = auth_schemas.TokenData(email=email, id=id)

    except JWTError:
        raise credentials_exception

    return token_data


def verify_access_token(token: str, credentials_exception, db: orm.Session):
    """Verify an assigned access token"""
    try:
        # check if token still exist
        check_token = (
            db.query(auth_models.Token).filter(auth_models.Token.token == token).first()
        )
        if check_token is None:
            raise fastapi.HTTPException(status_code=403, detail="Invalid Credentials")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        id: str = payload.get("user_id")
        user = db.query(user_models.User).filter(user_models.User.id == id).first()
        email = user.email
        if id is None:
            raise credentials_exception
        token_data = auth_schemas.TokenData(email=email, id=id)

        return token_data

    except JWTError:
        return JWTError(credentials_exception)


def is_authenticated(
    token: str = fastapi.Depends(oauth2_scheme),
    refresh_token: Union[str, None] = Cookie(default=None),
    db: orm.Session = fastapi.Depends(get_db),
):
    credentials_exception = fastapi.HTTPException(
        status_code=fastapi.status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if type(token) == str:
        access_token = verify_access_token(token, credentials_exception, db)

        if type(access_token) is JWTError:
            refresh_token = verify_refresh_token(
                refresh_token, credentials_exception, db
            )

            user = (
                db.query(user_models.User)
                .filter(user_models.User.id == refresh_token.id)
                .first()
            )

            return user

        user = (
            db.query(user_models.User)
            .filter(user_models.User.id == access_token.id)
            .first()
        )

        return user

    if type(token) == dict:
        app_id = token["APP_ID"]
        api_key = token["API_KEY"]
        user = check_api_key(app_id, api_key, db)

        return user


def valid_email_from_db(email, db: orm.Session = fastapi.Depends(get_db)):
    found_user = (
        db.query(user_models.User).filter(user_models.User.email == email).first()
    )
    return found_user


def generate_code(new_length: int = None):
    length = 6
    if new_length is not None:
        length = new_length
    if length < 4:
        raise fastapi.HTTPException(status_code=400, detail="Minimum code lenght is 4")
    code = ""
    for i in range(length):
        code += str(random.randint(0, 9))
    return code


async def create_verification_code(user: user_models.User, length: int = None):
    user_obj = users_schemas.User.from_orm(user)
    db = database.SessionLocal()
    code = ""
    db_code = await get_code_by_userid(user_id=user_obj.id, db=db)
    if db_code:
        db.delete(db_code)
        db.commit()
        code = generate_code(length)
        code_obj = auth_models.VerificationCode(
            id=uuid4().hex, user_id=user_obj.id, code=code
        )
        db.add(code_obj)
        db.commit()
        db.refresh(code_obj)
    else:
        code = generate_code(length)
        code_obj = auth_models.VerificationCode(
            id=uuid4().hex, user_id=user_obj.id, code=code
        )
        db.add(code_obj)
        db.commit()
        db.refresh(code_obj)

    return {"code": code}


async def create_forgot_pasword_code(
    user: users_schemas.UserRecoverPassword, length: int = None
):
    db = database.SessionLocal()
    user_obj = await get_user(db, email=user.email)
    print(user_obj)
    code = ""

    db_code = (
        db.query(auth_models.PasswordResetCode)
        .filter(auth_models.PasswordResetCode.user_id == user_obj.id)
        .first()
    )
    if db_code:
        db.delete(db_code)
        db.commit()
        code = generate_code(length)
        code_obj = auth_models.PasswordResetCode(
            id=uuid4().hex, user_id=user_obj.id, code=code
        )
        db.add(code_obj)
        db.commit()
        db.refresh(code_obj)
    else:
        code = generate_code(length)
        code_obj = auth_models.PasswordResetCode(
            id=uuid4().hex, user_id=user_obj.id, code=code
        )
        db.add(code_obj)
        db.commit()
        db.refresh(code_obj)

    return code


async def get_token_by_userid(user_id: str, db: orm.Session):
    return (
        db.query(auth_models.Token).filter(auth_models.Token.user_id == user_id).first()
    )


async def generate_verification_token(user_id: str, db: orm.Session):
    payload = {"user_id": user_id}
    token = JWT.encode(payload, JWT_SECRET, ALGORITHM)
    token_obj = auth_models.VerificationToken(
        id=uuid4().hex, user_id=user_id, token=token
    )
    db.add(token_obj)
    db.commit()
    db.refresh(token_obj)
    return token


async def create_verification_token(user: user_models.User):
    user_obj = users_schemas.User.from_orm(user)
    db = database.SessionLocal()
    token = ""

    db_token = (
        db.query(auth_models.VerificationToken)
        .filter(auth_models.VerificationToken.user_id == user_obj.id)
        .first()
    )
    if db_token:
        validate_resp = await verify_access_token(db_token.token)
        if not validate_resp["status"]:
            db.delete(db_token)
            db.commit()
            token = await generate_verification_token(user_obj.id, db)
        else:
            token = db_token.token
    else:
        token = await generate_verification_token(user_obj.id, db)

    return {"token": token}


async def generate_passwordreset_token(data: dict, db: orm.Session):
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(minutes=1440)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    token_obj = auth_models.PasswordResetToken(
        id=uuid4().hex, user_id=data["user_id"], token=encoded_jwt
    )
    db.add(token_obj)
    db.commit()
    db.refresh(token_obj)
    return encoded_jwt


async def create_passwordreset_token(user: user_models.User):
    user_obj = users_schemas.User.from_orm(user)
    db = database.SessionLocal()
    token = ""

    db_token = (
        db.query(auth_models.VerificationToken)
        .filter(auth_models.VerificationToken.user_id == user_obj.id)
        .first()
    )
    if db_token:
        validate_resp = await verify_access_token(db_token.token)
        if not validate_resp["status"]:
            db.delete(db_token)
            db.commit()
            token = await generate_verification_token(user_obj.id, db)
        else:
            token = db_token.token
    else:
        token = await generate_verification_token(user_obj.id, db)

    return {"token": token}


async def logout(user: users_schemas.User):
    db = database.SessionLocal()
    db_token = await get_token_by_userid(user_id=user.id, db=db)
    db.delete(db_token)
    db.commit()
    return True


async def password_change_code(
    password: users_schemas.UserPasswordUpdate, code: str, db: orm.Session
):

    code_db = await get_password_reset_code_from_db(code, db)
    if code_db:
        user = await get_user(db=db, id=code_db.user_id)
        user.password = _hash.sha256_crypt.hash(password.password)
        db.commit()
        db.refresh(user)

        db.delete(code_db)
        db.commit()
        return {"message": "password change successful"}


async def verify_user_token(token: str):
    db = database.SessionLocal()
    validate_resp = await verify_access_token(token)
    if not validate_resp["status"]:
        raise fastapi.HTTPException(status_code=401, detail=validate_resp["data"])

    user = await get_user(db=db, id=validate_resp["data"]["user_id"])
    user.is_verified = True

    db.commit()
    db.refresh(user)

    return users_schemas.User.from_orm(user)


async def password_change_token(
    password: users_schemas.UserPasswordUpdate, token: str, db: orm.Session
):
    validate_resp = await verify_access_token(token)
    if not validate_resp["status"]:
        raise fastapi.HTTPException(status_code=401, detail=validate_resp["data"])

    token_db = (
        db.query(auth_models.PasswordResetToken)
        .filter(auth_models.PasswordResetToken.token == token)
        .first()
    )
    if token_db:
        user = await get_user(db=db, id=validate_resp["data"]["user_id"])
        user.password = _hash.sha256_crypt.hash(password.password)
        db.commit()
        db.refresh(user)

        db.delete(token_db)
        db.commit()
        return {"message": "password change successful"}
    else:
        raise fastapi.HTTPException(status_code=401, detail="Invalid Token")


async def send_code_password_reset_email(
    email: str,
    db: orm.Session,
    background_tasks: BackgroundTasks,
    codelength: int = None,
):
    user = await get_user(db, email=email)
    if not user:
        raise fastapi.HTTPException(status_code=401, detail="Email not registered")

    code = await create_forgot_pasword_code(user, codelength)
    print(code)
    await email_services.send_email(
        recipients=[email],
        # user,
        background_tasks=background_tasks,
        template="password_reset.html",
        title="Password Reset",
        code=code,
    )
    return code


async def resend_code_verification_mail(
    email: str, db: orm.Session, codelength: int = None
):
    user = await get_user(db, email=email)
    if user:
        code = await create_verification_code(user, codelength)
        await email_services.send_email(
            email,
            user,
            template=settings.EMAIL_VERIFICATION_TEMPLATE,
            title="Account Verify",
            code=code,
        )
        return code
    else:
        raise fastapi.HTTPException(status_code=401, detail="Email not registered")


async def send_token_password_reset_email(
    email: str, redirect_url: str, db: orm.Session
):
    user = await get_user(db, email=email)
    if user:
        token = await create_passwordreset_token(user)
        path = "{}/?token={}".format(redirect_url, token["token"])
        await email_services.send_email(
            email,
            user,
            template=settings.PASSWORD_RESET_TEMPLATE,
            title="Change Your Password",
            path=path,
        )
        return {"token": token}
    else:
        raise fastapi.HTTPException(status_code=401, detail="Email not registered")


async def resend_token_verification_mail(
    email: str, redirect_url: str, db: orm.Session
):
    user = await get_user(db, email=email)
    if user:
        token = await create_verification_token(user)
        path = "{}/?token={}".format(redirect_url, token["token"])
        await email_services.send_email(
            email,
            user,
            template=settings.EMAIL_VERIFICATION_TEMPLATE,
            title="Verify Your Account",
            path=path,
        )
        return {"token": token}
    else:
        raise fastapi.HTTPException(status_code=401, detail="Email not registered")


async def get_code_by_userid(user_id: str, db: orm.Session):
    return (
        db.query(auth_models.VerificationCode)
        .filter(auth_models.VerificationCode.user_id == user_id)
        .first()
    )


async def get_password_reset_code_from_db(code: str, db: orm.Session):
    return (
        db.query(auth_models.PasswordResetCode)
        .filter(auth_models.PasswordResetCode.code == code)
        .first()
    )


# function to get user by email or id
async def get_user(db: orm.Session, email: str = "", id: str = ""):
    response = ""
    if id != "":
        response = db.query(user_models.User).filter(user_models.User.id == id).first()
    if email != "":
        response = (
            db.query(user_models.User).filter(user_models.User.email == email).first()
        )

    return response


def send_slack_notification(user):
    message = "New login from " + user.email
    # sends the message to slack
    Helpers.slack_notification("LOG_WEBHOOK_URL", text=message)


async def sync_user(
    user: auth_schemas.UserCreate,
    db: orm.Session,
    is_su: bool = False,
    is_active: bool = True,
):
    su_status = True if is_su else False

    # retrieve by email, separate list to update from list to insert
    # update all, insert all, id as pk

    existing_user = (
        db.query(user_models.User).filter(user_models.User.id == user.id).first()
    )

    if existing_user:
        existing_user.email = user.email
        try:
            db.commit()
        except:
            db.rollback()
        return {
            "data": auth_schemas.UserCreateOut.from_orm(existing_user),
            "updated": True,
        }

    if not user.id:
        user.id = uuid4().hex
    user_obj = user_models.User(
        id=user.id,
        email=user.email,
        password_hash=_hash.sha256_crypt.hash(user.password),
        first_name=user.first_name,
        last_name=user.last_name,
        phone_number=user.phone_number,
        is_active=is_active,
        is_verified=True,
        is_superuser=su_status,
        phone_country_code=user.phone_country_code,
        is_deleted=False,
        google_id=user.google_id,
        google_image_url=user.google_image_url,
        image_url=user.image_url,
        device_id=user.device_id,
    )

    try:
        db.add(user_obj)
        db.commit()
        db.refresh(user_obj)
        return {"data": auth_schemas.UserCreateOut.from_orm(user_obj), "updated": False}
    except:
        db.rollback()
        # raise Exception or print error


async def create_device_token(user, db: orm.Session):
    device_token = (
        db.query(auth_models.DeviceToken)
        .filter(auth_models.DeviceToken.device_id == user.device_id)
        .first()
    )

    if not device_token or device_token.max_age <= datetime.utcnow():
        device_token = auth_models.DeviceToken(
            device_id=user.device_id, user_email=user.email, token=uuid4().hex
        )
        db.add(device_token)
        db.commit()
        db.refresh(device_token)

    return device_token


async def get_device_token(device_id: str, device_token: str, db: orm.Session):
    device_credentials = (
        db.query(auth_models.DeviceToken)
        .filter(
            and_(
                auth_models.DeviceToken.device_id == device_id,
                auth_models.DeviceToken.token == device_token,
            )
        )
        .first()
    )

    if device_credentials.max_age <= datetime.utcnow():
        raise fastapi.HTTPException(
            status_code=401, detail="Device token expired, generate a new one"
        )

    return device_credentials
