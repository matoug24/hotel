from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Date, Boolean
from sqlalchemy.orm import relationship
# Import new time helper (assuming it's importable, or we use a lambda)
from core import get_current_time 
from database import Base

# Note: We can't easily pass the function itself as a default to SQLAlchmey column without it being a callable.
# We will use 'default=get_current_time' in the logic, but for schema definitions, 
# standard practice is usually server_default=func.now() or client side.
# Here we will rely on the routers passing the time or use a wrapper.

class SiteConfig(Base):
    __tablename__ = "site_config"
    id = Column(Integer, primary_key=True, index=True)
    extension = Column(String, unique=True, index=True)
    hotel_name = Column(String, default="Azure Horizon Beach Hotel")
    highlights = Column(String, default="Experience paradise.")
    about_description = Column(Text, default="Welcome to Azure Horizon.")
    amenities_list = Column(Text, default="Free Wi-Fi\nPool")
    rules = Column(Text, default="Check-in: 2PM.")
    contact_email = Column(String, default="info@example.com")
    contact_phone = Column(String, default="+1 555 0199")
    address = Column(String, default="123 Ocean Drive")
    map_url = Column(Text, default="")
    facebook = Column(String, default="#")
    instagram = Column(String, default="#")
    youtube = Column(String, default="#")
    is_active = Column(Boolean, default=True)
    theme_id = Column(Integer, default=1)
    booking_expiration_hours = Column(Integer, default=24)
    admin_password_hash = Column(String)
    booking_success_message = Column(Text, default="Please contact us within 24 hours to confirm your reservation.")
    
    users = relationship("User", back_populates="config", cascade="all, delete-orphan")
    rooms = relationship("RoomType", back_populates="config", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="config", cascade="all, delete-orphan")
    images = relationship("HeroImage", back_populates="config", cascade="all, delete-orphan")
    seasons = relationship("SeasonalRate", back_populates="config", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="config", cascade="all, delete-orphan")
    visitors = relationship("Visitor", back_populates="config", cascade="all, delete-orphan")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    username = Column(String, index=True)
    password_hash = Column(String)
    role = Column(String)
    config = relationship("SiteConfig", back_populates="users")

class HeroImage(Base):
    __tablename__ = "hero_images"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    image_url = Column(String)
    config = relationship("SiteConfig", back_populates="images")

class RoomType(Base):
    __tablename__ = "room_types"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    name = Column(String, index=True)
    description = Column(Text)
    price_per_night = Column(Float)
    total_quantity = Column(Integer)
    capacity = Column(Integer, default=2)
    config = relationship("SiteConfig", back_populates="rooms")
    images = relationship("RoomImage", back_populates="room", cascade="all, delete-orphan")
    units = relationship("RoomUnit", back_populates="room_type", cascade="all, delete-orphan")
    seasons = relationship("SeasonalRate", back_populates="room", cascade="all, delete-orphan")

class RoomUnit(Base):
    __tablename__ = "room_units"
    id = Column(Integer, primary_key=True, index=True)
    room_type_id = Column(Integer, ForeignKey("room_types.id"))
    label = Column(String) 
    room_type = relationship("RoomType", back_populates="units")
    bookings = relationship("Booking", back_populates="assigned_unit")

class RoomImage(Base):
    __tablename__ = "room_images"
    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("room_types.id"))
    image_url = Column(String)
    room = relationship("RoomType", back_populates="images")

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    booking_code = Column(String, index=True)
    room_type_id = Column(Integer, ForeignKey("room_types.id"))
    room_unit_id = Column(Integer, ForeignKey("room_units.id"), nullable=True)
    guest_name = Column(String)
    guest_email = Column(String, nullable=True)
    guest_phone = Column(String, nullable=True)
    check_in = Column(DateTime)
    check_out = Column(DateTime)
    rooms_booked = Column(Integer, default=1)
    guests_count = Column(Integer, default=1)
    status = Column(String, default="pending") 
    created_at = Column(DateTime, default=get_current_time) # Uses Libya Time
    total_price = Column(Float, default=0.0)
    deposit_amount = Column(Float, default=0.0)
    notes = Column(Text, default="") 
    config = relationship("SiteConfig", back_populates="bookings")
    room = relationship("RoomType")
    assigned_unit = relationship("RoomUnit", back_populates="bookings")

class SeasonalRate(Base):
    __tablename__ = "seasonal_rates"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    room_type_id = Column(Integer, ForeignKey("room_types.id"), nullable=True)
    name = Column(String)
    start_date = Column(Date)
    end_date = Column(Date)
    multiplier = Column(Float, default=1.0)
    config = relationship("SiteConfig", back_populates="seasons")
    room = relationship("RoomType", back_populates="seasons")

class MaintenanceBlock(Base):
    __tablename__ = "maintenance_blocks"
    id = Column(Integer, primary_key=True, index=True)
    room_type_id = Column(Integer, ForeignKey("room_types.id"))
    room_unit_id = Column(Integer, ForeignKey("room_units.id"), nullable=True)
    start_date = Column(Date)
    end_date = Column(Date)
    reason = Column(String)
    qty_blocked = Column(Integer, default=1)
    room = relationship("RoomType")
    unit = relationship("RoomUnit")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    timestamp = Column(DateTime, default=get_current_time) # Uses Libya Time
    user = Column(String)
    action = Column(String)
    target = Column(String)
    details = Column(Text)
    config = relationship("SiteConfig", back_populates="audit_logs")

class Visitor(Base):
    __tablename__ = "visitors"
    id = Column(Integer, primary_key=True, index=True)
    site_config_id = Column(Integer, ForeignKey("site_config.id"))
    ip_address = Column(String)
    user_agent = Column(String)
    path = Column(String)
    timestamp = Column(DateTime, default=get_current_time) # Uses Libya Time
    
    config = relationship("SiteConfig", back_populates="visitors")
