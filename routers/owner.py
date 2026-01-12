from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse, HTMLResponse
import models
from database import get_db
from core import templates, verify_owner, log_activity, pwd_context, SECRET_KEY, ALGORITHM
from jose import jwt, JWTError

router = APIRouter()

@router.get("/owner")
def owner_dashboard(request: Request, db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    configs = db.query(models.SiteConfig).all(); msg = request.query_params.get("success")
    return templates.TemplateResponse("owner.html", {"request": request, "configs": configs, "msg": msg})

@router.post("/owner/create_hotel")
def create_hotel(extension: str = Form(...), name: str = Form(...), admin_pass: str = Form(...), user_pass: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    if db.query(models.SiteConfig).filter(models.SiteConfig.extension == extension).first(): return "Extension exists"
    new_conf = models.SiteConfig(extension=extension, hotel_name=name)
    db.add(new_conf); db.commit()
    db.add_all([models.User(site_config_id=new_conf.id, username=f"{extension}_ad", password_hash=pwd_context.hash(admin_pass), role="admin"), models.User(site_config_id=new_conf.id, username=f"{extension}_user", password_hash=pwd_context.hash(user_pass), role="staff")]); db.commit()
    return RedirectResponse(url="/owner?success=Hotel+Created", status_code=303)

@router.post("/owner/reset_password")
def reset_hotel_password(config_id: int = Form(...), role: str = Form(...), db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    user = db.query(models.User).filter(models.User.site_config_id == config_id, models.User.role == role).first()
    if user: user.password_hash = pwd_context.hash("ResetToday"); log_activity(db, config_id, "Owner", "Password Reset", f"{role} User", "Reset"); db.commit(); return RedirectResponse(url="/owner?success=Password+Reset", status_code=303)
    return RedirectResponse(url="/owner?error=User+Not+Found", status_code=303)

@router.get("/logout")
def logout(request: Request, db=Depends(get_db)):
    token = request.cookies.get("access_token")
    extension = None

    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            config_id = payload.get("config_id")
            if config_id:
                cfg = db.query(models.SiteConfig).filter(models.SiteConfig.id == config_id).first()
                if cfg:
                    extension = cfg.extension
        except JWTError:
            pass

    # Choose a safe default destination
    target = f"/app/{extension}/login" if extension else "/"

    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie("access_token", path="/")
    # response.delete_cookie("csrf_token", path="/")  # if you implement CSRF as above
    return response