from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from db import Base

class User(Base):
    __tablename__ = "shein_api_users"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "email", name="uq_shein_owner_email"),
    )

    id = Column(Integer, primary_key=True)
    owner_user_id = Column(Integer, nullable=True, index=True)
    email = Column(String(255), nullable=False, index=True)

    gmail_email = Column(String(255), nullable=False)
    gmail_app_password_enc = Column(Text, nullable=False)

    shein_email = Column(String(255), nullable=False)
    shein_password_enc = Column(Text, nullable=False)

    shein_storage_state_enc = Column(Text, nullable=True)

    orders = relationship("Order", back_populates="user", cascade="all, delete")

class Order(Base):
    __tablename__ = "shein_api_orders"
    __table_args__ = (UniqueConstraint("user_id", "order_no", name="uniq_user_order"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    order_no = Column(String(64), nullable=False)
    carrier = Column(String(64), nullable=True)
    tracking_no = Column(String(64), nullable=True)
    status_text = Column(String(255), nullable=True)
    delivered = Column(Boolean, default=False)

    last_details = Column(Text, nullable=True)
    last_timestamp = Column(String(64), nullable=True)

    user = relationship("User", back_populates="orders")
