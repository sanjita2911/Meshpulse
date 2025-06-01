from fastapi import HTTPException
from fastapi import FastAPI, Request
import time
import random
import requests
from prometheus_client import make_asgi_app

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.trace import Status, StatusCode

from sqlalchemy import create_engine, Column, String, Float, DateTime
from sqlalchemy.orm import declarative_base, Session
from datetime import datetime
from pydantic import BaseModel
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://user:pass@postgres:5432/users_db")
engine = create_engine(DATABASE_URL)
Base = declarative_base()


class Payment(Base):
    __tablename__ = "payments"
    id = Column(String, primary_key=True)
    order_id = Column(String)
    user_id = Column(String)
    amount = Column(Float)
    status = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)


class PaymentIn(BaseModel):
    id: str
    order_id: str
    user_id: str
    amount: float


trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: "payments-service"})
    )
)
jaeger_exporter = JaegerExporter(agent_host_name="jaeger", agent_port=6831)
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(jaeger_exporter))

metrics.set_meter_provider(
    MeterProvider(metric_readers=[PrometheusMetricReader()])
)
meter = metrics.get_meter(__name__)
hist = meter.create_histogram("http.server.duration", unit="ms")
# counter+1 when we see this error
err_ctr = meter.create_counter("http.server.errors")
payment_total = meter.create_counter(
    "payments.total.amount",
    unit="USD",
    description="Total amount processed in payments"
)

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()


@app.post("/payments")
async def create_payment(payment: PaymentIn, request: Request):
    start = time.time()
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("create_payment") as span:
        span.set_attribute("payment.id", payment.id)
        span.set_attribute("payment.amount", payment.amount)
        span.set_attribute("user.id", payment.user_id)
        span.set_attribute("order.id", payment.order_id)

        # Step 1: Check if payment already exists
        with tracer.start_as_current_span("check_existing_payment"):
            with Session(engine) as session:
                existing = session.query(Payment).filter(
                    Payment.id == payment.id).first()
                if existing:
                    span.set_attribute("payment.duplicate", True)
                    raise HTTPException(
                        status_code=400, detail="Payment ID already exists")

        # Step 2: Validate the order
        with tracer.start_as_current_span("validate_order_id") as validate_order_span:
            try:
                order_resp = requests.get(
                    f"http://orders-service:8002/orders/status/{payment.order_id}", timeout=2
                )
                order_resp.raise_for_status()
                order_data = order_resp.json()
                validate_order_span.set_attribute("order.found", True)
            except Exception:
                validate_order_span.set_status(Status(StatusCode.ERROR))
                validate_order_span.set_attribute("order.found", False)
                raise HTTPException(status_code=404, detail="Invalid order ID")

        # Step 3: Check if order belongs to user
        with tracer.start_as_current_span("check_order_user_match") as match_span:
            if order_data["user_id"] != payment.user_id:
                match_span.set_status(Status(StatusCode.ERROR))
                match_span.set_attribute("order.user.mismatch", True)
                raise HTTPException(
                    status_code=400, detail="Order does not belong to user")
            match_span.set_attribute("order.user.mismatch", False)

        # Step 4: Validate the user
        with tracer.start_as_current_span("validate_user") as validate_user_span:
            try:
                user_resp = requests.get(
                    f"http://user-service:8001/users/{payment.user_id}", timeout=2
                )
                user_resp.raise_for_status()
                validate_user_span.set_attribute("user.found", True)
            except Exception:
                validate_user_span.set_status(Status(StatusCode.ERROR))
                validate_user_span.set_attribute("user.found", False)
                raise HTTPException(status_code=404, detail="Invalid user ID")

        # Step 5: Simulate processing
        time.sleep(0.1)
        status = "Success" if random.random() > 0.05 else "Failed"
        span.set_attribute("payment.status", status)
        if status == "Success":
            payment_total.add(payment.amount, {"status": status})

        # Step 6: Insert into DB
        with tracer.start_as_current_span("insert_payment_db"):
            with Session(engine) as session:
                new_payment = Payment(
                    id=payment.id,
                    order_id=payment.order_id,
                    user_id=payment.user_id,
                    amount=payment.amount,
                    status=status
                )
                session.add(new_payment)
                session.commit()

        hist.record((time.time() - start) * 1000, {"route": "/payments"})
        return {"message": "Processed", "status": status, "payment_id": payment.id}


@app.get("/payments/status/{order_id}")
async def get_payment_status(order_id: str, request: Request):
    start = time.time()
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("get_payment_status") as span:
        with Session(engine) as session:
            payment = session.query(Payment).filter(
                Payment.order_id == order_id).first()

        hist.record((time.time() - start) * 1000,
                    {"route": "/payments/status/{order_id}"})

        if payment:
            return {
                "order_id": order_id,
                "status": payment.status,
                "payment_id": payment.id,
                "amount": payment.amount
            }
        else:
            return {
                "order_id": order_id,
                "status": "Not Paid"
            }


# Expose /metrics endpoint for Prometheus scraping
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
