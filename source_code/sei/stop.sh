pid=`ps -ef | grep seid | grep -v grep |  awk  '{print $2}'`
if [ ! -z ${pid} ];
then
    kill -9 $pid
    echo "seid is stopping..."
fi