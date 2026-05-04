// VICTIM: org.wikidata.wdtk.util.DirectoryManagerFactoryTest.createDefaultDirectoryManagerPath
	@Test
	public void createDefaultDirectoryManagerPath() throws IOException {
		Path path = Paths.get(System.getProperty("user.dir"));
		DirectoryManagerFactory.setDirectoryManagerClass(DirectoryManagerImpl.class);
		DirectoryManager dm = DirectoryManagerFactory.createDirectoryManager(
				path, true);
		assertTrue(dm instanceof DirectoryManagerImpl);
		DirectoryManagerImpl dmi = (DirectoryManagerImpl) dm;
		assertTrue(dmi.readOnly);
		assertEquals(path, dmi.directory);
	}

// POLLUTER: org.wikidata.wdtk.util.DirectoryManagerFactoryTest.createDirectoryManagerNoConstructor
	@Test
	public void createDirectoryManagerNoConstructor() throws IOException {
		DirectoryManagerFactory
				.setDirectoryManagerClass(TestDirectoryManager.class);
		DirectoryManagerFactory.createDirectoryManager("/", true);
		DirectoryManagerFactory.setDirectoryManagerClass(DirectoryManagerImpl.class);
	}