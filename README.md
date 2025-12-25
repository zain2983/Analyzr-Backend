# Analyzr Backend

This repository contains the backend service for **Analyzr**.

The backend is built solely to support the frontend by handling CPU-intensive operations such as data processing and analysis.

## Overview

- The service operates entirely in memory
- No database is used
- No data is persisted
- All data exists only for the lifetime of the process

If the server restarts, all data is intentionally lost.

## Design Purpose

This backend is intentionally lightweight and stateless. Its primary goals are:

- Offloading heavy computation from the frontend
- Keeping the architecture simple
- Avoiding unnecessary persistence and infrastructure overhead

The backend is not intended to be used as a standalone service.

## Frontend

The frontend application is available at:

https://analyzr-z1.vercel.app
