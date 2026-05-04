@Before
public void init() {
    FilterContainer.getInstance().clear();
}

@Test
public void testIsOkExcludeFalse() {
    SourcecodeFilter filter = new SourcecodeFilter();
    filter.setFilterToken(".ag XYZ oncal.");
    filter.setExclude(true);
    FilterContainer.getInstance().add(filter);

    assertEquals(true, FilterContainer.getInstance().isOk(javaSource));
}

@Test
public void testIsOkExcludeTrue() {
    SourcecodeFilter filter = new SourcecodeFilter();
    filter.setFilterToken(".agoncal.");
    filter.setExclude(true);
    FilterContainer.getInstance().add(filter);

    assertEquals(false, FilterContainer.getInstance().isOk(javaSource));
}