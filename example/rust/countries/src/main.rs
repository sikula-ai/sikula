mod cli;
mod commands;

use anyhow::Context;
use clap::Parser;

use cli::{Cli, Command};
use countries::data::loader;

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    let countries = loader::load_countries(&cli.data)
        .with_context(|| format!("could not load data from {}", cli.data.display()))?;

    let result = match &cli.command {
        Command::List(args) => commands::list::run(args, &countries),
        Command::Search(args) => commands::search::run(args, &countries),
        Command::Info(args) => commands::info::run(args, &countries),
        Command::Stats(args) => commands::stats::run(args, &countries),
    };

    result.with_context(|| "command failed")?;

    Ok(())
}
