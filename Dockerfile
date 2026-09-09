# The deployment host already has this image cached with Python 3.11. Using it
# keeps the console deployable when Docker Hub is unreachable.
FROM ghcr.io/chatmail/docker:main

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
RUN addgroup --system app && adduser --system --ingroup app app
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT','8080') + '/healthz', timeout=3)"
CMD ["python", "app.py"]
ENTRYPOINT []
