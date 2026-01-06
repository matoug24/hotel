from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse, HTMLResponse
import models
from database import get_db
from core import templates, verify_owner, log_activity, pwd_context, OWNER_HASH

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

# GLOBAL LOGOUT (Handled in Owner Router as it is general)
@router.get("/logout")
def logout(request: Request):
    referer = request.headers.get("referer")
    redirect_url = "/"
    if referer and "/app/" in referer:
        try: redirect_url = "/app/" + referer.split("/app/")[1].split("/")[0]
        except: pass
    elif referer and "owner" in referer: redirect_url = "/owner_login"
    
    # Poison Cache if Admin (using the bypass logic)
    if referer and "/admin" in referer:
        html_content = f"""<!DOCTYPE html><html><head><title>Logging Out...</title></head><body><h3>Logging out...</h3><script>var xhr=new XMLHttpRequest();xhr.open("GET", "{referer}/logout_bypass", true);xhr.setRequestHeader("Authorization","Basic "+btoa("logout:logout"));xhr.onreadystatechange=function(){{if(xhr.readyState==4){{window.location.href="{redirect_url}";}}}};xhr.send();</script></body></html>"""
        response = HTMLResponse(content=html_content)
    else:
        response = RedirectResponse(url=redirect_url, status_code=303)
    
    response.delete_cookie("access_token")
    return response

@router.get("/reset_db")
def reset_db(db: Session = Depends(get_db), auth: bool = Depends(verify_owner)):
    db.query(models.AuditLog).delete(); db.query(models.Booking).delete(); db.query(models.RoomImage).delete()
    db.query(models.MaintenanceBlock).delete(); db.query(models.SeasonalRate).delete()
    db.query(models.RoomUnit).delete(); db.query(models.RoomType).delete(); db.query(models.HeroImage).delete(); db.query(models.SiteConfig).delete(); db.query(models.User).delete()
    default_hash = pwd_context.hash("password123")
    config = models.SiteConfig(admin_password_hash=default_hash, booking_expiration_hours=24, highlights="Experience paradise.", about_description="Welcome.", amenities_list="Free Wi-Fi")
    db.add(config); db.commit()
    return "Database cleared & Updated!"
