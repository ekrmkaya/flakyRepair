@Test
public void assertGetCurrentShardingTotalCountIfNull() {
    JobRegistry.getInstance().setCurrentShardingTotalCount("exist_job_instance", 0);
    assertThat(JobRegistry.getInstance().getCurrentShardingTotalCount("exist_job_instance"), is(0));
}

@Test
public void assertGetCurrentShardingTotalCountIfNotNull() {
    JobRegistry.getInstance().setCurrentShardingTotalCount("exist_job_instance", 10);
    assertThat(JobRegistry.getInstance().getCurrentShardingTotalCount("exist_job_instance"), is(10));
    JobRegistry.getInstance().setCurrentShardingTotalCount("exist_job_instance", 0);
}