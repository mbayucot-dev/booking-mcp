"""Reusable prompt templates."""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.prompt(tags={"booking"})
    def book_cleaning(customer: str, service: str, date: str, time: str, email: str) -> str:
        """A natural-language booking request (e.g. to feed the booking agent)."""
        return f"Create a booking for {customer} for {service} on {date} at {time}. Email {email}."

    @mcp.prompt(tags={"booking"})
    def summarize_schedule(date: str) -> str:
        """Ask the model to summarize a day's schedule from the schedule resource."""
        return (
            f"Read the resource booking://schedule/{date} and summarize the day: who is "
            "booked, at what times, and any obvious gaps or overloaded cleaners."
        )
