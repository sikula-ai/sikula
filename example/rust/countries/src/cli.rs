use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};

use countries::domain::country::CountryCode;

/// A CLI tool for exploring world country data.
#[derive(Debug, Parser)]
#[command(name = "countries", version, about, long_about = None)]
pub struct Cli {
    /// Path to the countries JSON data file.
    #[arg(
        long,
        global = true,
        default_value = "data/countries.json",
        env = "COUNTRIES_DATA"
    )]
    pub data: PathBuf,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// List all countries, optionally filtered by region.
    List(ListArgs),

    /// Search for countries by name (partial, case-insensitive).
    Search(SearchArgs),

    /// Show detailed information for a single country by its alpha-3 code.
    Info(InfoArgs),

    /// Show aggregate statistics, optionally scoped to a region.
    Stats(StatsArgs),
}

#[derive(Debug, Args)]
pub struct ListArgs {
    /// Only show countries in this region (e.g. "Europe").
    #[arg(long)]
    pub region: Option<String>,
}

#[derive(Debug, Args)]
pub struct SearchArgs {
    /// Fragment to search for in country names.
    #[arg(long)]
    pub name: String,
}

#[derive(Debug, Args)]
pub struct InfoArgs {
    /// ISO 3166-1 alpha-3 country code (e.g. CZE).
    pub code: CountryCode,
}

#[derive(Debug, Args)]
pub struct StatsArgs {
    /// Restrict statistics to this region.
    #[arg(long)]
    pub region: Option<String>,
}
