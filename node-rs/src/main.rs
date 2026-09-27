fn main() {
    println!("ramen-node {}", env!("CARGO_PKG_VERSION"));
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_is_semver() {
        assert_eq!(env!("CARGO_PKG_VERSION").split('.').count(), 3);
    }
}
