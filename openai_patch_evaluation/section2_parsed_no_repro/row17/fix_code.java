@Test
public void assertWithoutEventTraceRdbConfiguration() {
    Properties properties = new Properties();
    ReflectionUtils.setFieldValue(bootstrapEnvironment, "properties", properties);
    assertFalse(bootstrapEnvironment.getTracingConfiguration().isPresent());
}

@Test
public void assertGetEventTraceRdbConfigurationMap() {
    Properties properties = new Properties();
    properties.setProperty(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_DRIVER.getKey(), "org.h2.Driver");
    properties.setProperty(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_URL.getKey(), "jdbc:h2:mem:job_event_trace");
    properties.setProperty(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_USERNAME.getKey(), "sa");
    properties.setProperty(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_PASSWORD.getKey(), "password");
    ReflectionUtils.setFieldValue(bootstrapEnvironment, "properties", properties);
    Map<String, String> jobEventRdbConfigurationMap = bootstrapEnvironment.getJobEventRdbConfigurationMap();
    assertThat(jobEventRdbConfigurationMap.get(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_DRIVER.getKey()), is("org.h2.Driver"));
    assertThat(jobEventRdbConfigurationMap.get(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_URL.getKey()), is("jdbc:h2:mem:job_event_trace"));
    assertThat(jobEventRdbConfigurationMap.get(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_USERNAME.getKey()), is("sa"));
    assertThat(jobEventRdbConfigurationMap.get(BootstrapEnvironment.EnvironmentArgument.EVENT_TRACE_RDB_PASSWORD.getKey()), is("password"));
}