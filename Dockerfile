FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0
ENV PORT=8050
ENV PLOTLYOFFICE_DATABASE=/app/data/office.sqlite3
ENV PLOTLYSHEETS_DATABASE=/app/data/sheets.sqlite3
ENV PLOTLYSLIDES_DATABASE=/app/data/slides.sqlite3
ENV PLOTLYWORD_DATABASE=/app/data/documents.sqlite3

EXPOSE 8050

CMD ["uvicorn", "app:server", "--host", "0.0.0.0", "--port", "8050", "--workers", "1"]
