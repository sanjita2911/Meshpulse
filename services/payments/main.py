from fastapi import FastAPI, Request
import time, random, requests
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

# First, we set up the tracing

trace.set_tracer_provider(
    TracerProvider(
        resource=Resource.create({SERVICE_NAME: "payments-service"})  # Change per service
    )
)
jaeger_exporter = JaegerExporter(agent_host_name="jaeger", agent_port=6831)
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(jaeger_exporter))

# Then, monitoring using Prometheus
metrics.set_meter_provider(
    MeterProvider(metric_readers=[PrometheusMetricReader()])
)
meter = metrics.get_meter(__name__)
hist = meter.create_histogram("http.server.duration", unit="ms")
err_ctr = meter.create_counter("http.server.errors") # counter+1 when we see this error

app = FastAPI()
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()

@app.get("/payments/{user_id}")
async def get_payment(user_id: str, request: Request):
    start = time.time()
    try:
        # Call Orders service to get user and orders info
        resp = requests.get(f"http://orders-service:8002/orders/{user_id}", timeout=2)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        err_ctr.add(1, {"route": "/payments/{user_id}"})
        raise Exception("Failed to fetch orders info")

    # Simulate random failure (5% chance)
    if random.random() < 0.05:
        err_ctr.add(1, {"route": "/payments/{user_id}"})
        raise Exception("Simulated payment lookup failure")

    # Simulate payment lookup latency
    time.sleep(0.09)

    duration = (time.time() - start) * 1000
    hist.record(duration, {"route": "/payments/{user_id}"})

    # Return combined result
    return {
        "user": data["user"],
        "orders": data["orders"],
        "payment": {"payment_id": 1, "status": "Success"}
    }

# Expose /metrics endpoint for Prometheus scraping
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)