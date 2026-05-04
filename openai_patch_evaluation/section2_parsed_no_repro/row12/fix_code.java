@Before
public void setUp() throws Exception {
	instance.reset();
	instance.registerParser("VDM", VDMParser.class);
}

@Test
public void testParametrizedConstructor() {

	ExtendedBasicListener ebl = new ExtendedBasicListener();

	assertNull(ebl.get());
	assertEquals(ebl.messageType, AISMessage01.class);
}

@Test
public void testRegisterParserWithAlternativeBeginChar() {

	try {
		assertTrue(instance.hasParser("VDM"));
	} catch (Exception e) {
		fail("parser registering failed");
	}

	Sentence s = instance.createParser("!AIVDM,1,2,3");
	assertNotNull(s);
	assertTrue(s instanceof Sentence);
	assertTrue(s instanceof SentenceParser);
	assertTrue(s instanceof VDMParser);
	instance.unregisterParser(VDMParser.class);
	assertFalse(instance.hasParser("VDM"));
	instance.registerParser("VDM", VDMParser.class);
}