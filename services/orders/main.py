from fastapi import FastAPI, Request
import time, random, requests
from prometheus_client import make_asgi_app
from sqlalchemy import create_engine, Column, String, Float, DateTime
from sqlalchemy.orm import declarative_base, Session
from datetime import datetime
from pydantic import BaseModel
import os

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from fastapi import HTTPException


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@postgres:5432/users_db")
engine = create_engine(DATABASE_URL)
Base = declarative_base()

class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    user_id = Column(String)
    item = Column(String)
    price = Column(Float)
    status = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

class OrderIn(BaseModel):
    id: str
    user_id: str
    item: str
    price: float


trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create({SERVICE_NAME: "orders-service"})
    )
)
jaeger_exporter = JaegerExporter(agent_host_name="jaeger", agent_port=6831)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(jaeger_exporter))

tracer = trace.get_tracer(__name__) 

# Then, monitoring using Prometheus

metrics.set_meter_provider(
    MeterProvider(metric_readers=[PrometheusMetricReader()])
)
meter = metrics.get_meter(__name__)
hist = meter.create_histogram("http.server.duration", unit="ms")
err_ctr = meter.create_counter("http.server.errors") # counter+1 when we see this error

#Create app route and wrap it around oTel
app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()

@app.get("/orders/{user_id}")
async def get_orders(user_id: str, request: Request):
    start = time.time()

    with tracer.start_as_current_span("get_orders_for_user") as span:
        span.set_attribute("user_id", user_id)

        # Step 1: Verify user exists via user-service
        try:
            resp = requests.get(f"http://user-service:8001/users/{user_id}", timeout=2)
            resp.raise_for_status()
            user = resp.json()
        except Exception as e:
            span.set_attribute("error", True)
            err_ctr.add(1, {"route": "/orders/{user_id}"})
            raise Exception("Failed to fetch user info") from e

        # Step 2: Query orders DB
        with Session(engine) as session:
            orders = session.query(Order).filter(Order.user_id == user_id).all()
            span.set_attribute("db.rowcount", len(orders))
            order_data = [
                {
                    "id": o.id,
                    "item": o.item,
                    "price": o.price,
                    "status": o.status,
                    "created_at": o.created_at.isoformat()
                } for o in orders
            ]

        # Optional: Inject random failure to test Jaeger
        if random.random() < 0.05:
            span.set_attribute("simulated_failure", True)
            err_ctr.add(1, {"route": "/orders/{user_id}"})
            raise Exception("Simulated order lookup failure")

        # Record latency
        duration = (time.time() - start) * 1000
        hist.record(duration, {"route": "/orders/{user_id}"})

        return {
            "user": user,
            "orders": order_data
        }
        
@app.post("/orders", status_code=201)
async def create_order(order: OrderIn, request: Request):
    start = time.time()

    with tracer.start_as_current_span("create_order") as span:
        span.set_attribute("user_id", order.user_id)
        span.set_attribute("item", order.item)
        span.set_attribute("price", order.price)

        # Step 1: Validate user by calling user-service
        try:
            user_resp = requests.get(f"http://user-service:8001/users/{order.user_id}", timeout=2)
            user_resp.raise_for_status()
        except Exception as e:
            span.set_attribute("user_validation_failed", True)
            err_ctr.add(1, {"route": "/orders"})
            raise HTTPException(status_code=400, detail="Invalid user ID") from e

        # Step 2: Save order to DB
        with Session(engine) as session:
            new_order = Order(
                id=order.id,
                user_id=order.user_id,
                item=order.item,
                price=order.price,
                status="pending"  # default status
            )
            session.add(new_order)
            session.commit()

        duration = (time.time() - start) * 1000
        hist.record(duration, {"route": "/orders"})

        return {"message": "Order created", "order_id": order.id}        
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
