from rich.console import Console

console = Console()


def title(text: str):
    console.print(f"\n[bold cyan]{text}[/bold cyan]")


def success(text: str):
    console.print(f"[green]✓[/green] {text}")


def warning(text: str):
    console.print(f"[yellow]![/yellow] {text}")


def error(text: str):
    console.print(f"[red]✗[/red] {text}")


def line(text: str = ""):
    console.print(text)