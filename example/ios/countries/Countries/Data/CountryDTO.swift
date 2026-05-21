struct CountryDTO: Decodable {
    struct Name: Decodable {
        let common: String
    }

    let name: Name
    let cca2: String
    let capital: [String]?
    let region: String
    let population: Int
}
