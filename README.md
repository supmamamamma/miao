---
title: Vertex to Gemini Proxy
emoji: 🚀
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
secrets:
  - PROXY_API_KEY
  - VERTEX_EXPRESS_KEYS
---

# Vertex to Gemini Proxy

This Hugging Face Space hosts a FastAPI application that acts as a proxy between a Vertex AI Express endpoint and the Gemini API.

## Features

- **Authentication**: Protects the proxy with an API key.
- **Key Rotation**: Rotates through a list of Vertex Express keys.
- **Project ID Extraction**: Automatically determines the Google Cloud Project ID from the Vertex Express key.
- **Dynamic Proxy**: Forwards requests to the appropriate Gemini model and function.
- **Streaming Support**: Handles streaming responses from the Gemini API.
- **Model-Specific Logic**: Modifies request bodies for specific models as needed.

## Usage

1.  Set the `PROXY_API_KEY` and `VERTEX_EXPRESS_KEYS` secrets in your Hugging Face Space settings.
2.  Make requests to the Space URL, following the Gemini API format.
3.  Provide the `PROXY_API_KEY` in the `x-goog-api-key` header or as a `key` query parameter.