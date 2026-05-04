@Before
public void setUp() {
    FilterContainer.getInstance().clear();
}

@Test
public void testIsOkExcludeFalse() {
    PackageFilter filter = new PackageFilter();
    filter.setFilterToken("XYZpetstore");
    filter.setExclude(true);
    FilterContainer.getInstance().add(filter);

    assertEquals(true, FilterContainer.getInstance().isOk(javaSource));
}

@Test
public void testIsOkExcludeTrue() {
    PackageFilter filter = new PackageFilter();
    filter.setFilterToken("petstore");
    filter.setExclude(true);
    FilterContainer.getInstance().add(filter);

    assertEquals(false, FilterContainer.getInstance().isOk(javaSource));
}