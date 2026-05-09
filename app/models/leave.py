# app/models/leave.py
class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    
    id          = Column(Integer, primary_key=True)
    emp_code    = Column(String(20), index=True)
    emp_name    = Column(String(100))
    department  = Column(String(100))
    dates       = Column(Text)          # JSON list ["2026-05-10", "2026-05-11", ...]
    reason      = Column(Text, default="")
    status      = Column(String(20), default="pending")  # pending | approved | rejected
    submitted_at = Column(DateTime, default=datetime.now)
    reviewed_at  = Column(DateTime, nullable=True)
    reviewed_by  = Column(String(100), nullable=True)    # email của manager/admin duyệt
    note         = Column(Text, default="")              # ghi chú từ manager khi duyệt/từ chối