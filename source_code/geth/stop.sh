pid=`ps -ef | grep node2 | grep -v grep |  awk  '{print $2}'`
if [ ! -z ${pid} ];
then
    kill $pid
    echo "geth is stopping..."
fi