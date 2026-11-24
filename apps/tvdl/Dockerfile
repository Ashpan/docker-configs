FROM python:3.12-slim
WORKDIR /app
COPY server.py .
RUN useradd -u 1000 -m appuser
USER appuser
EXPOSE 8080
CMD ["python", "server.py", "8080"]
